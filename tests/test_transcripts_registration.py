from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from cc_transcript.ids import SessionId
from click.testing import CliRunner
from pydantic import ValidationError

from captain_hook.app import on
from captain_hook.cli import cli, dispatch_event
from captain_hook.session import SessionSlot, ensure_session
from captain_hook.snapshots.client import EvidenceIncomplete, SnapshotProtocolError
from captain_hook.state import ECHO_WINDOW, RegisteredTranscripts, UnbornTranscript
from captain_hook.testing.fixtures import T
from captain_hook.transcripts import (
    TranscriptLoadError,
    claim_unborn,
    record_unborn,
    register_transcript,
    registered_paths,
    resolved_transcript_paths,
)
from captain_hook.types import Event, Signal, Signals
from tests.helpers import raw_assistant, raw_text, raw_text_block, raw_tool_use

APPLY_PATCH_ENVELOPE = (
    "*** Begin Patch\n"
    "*** Update File: src/a.py\n"
    "@@\n"
    "-x\n"
    "+y\n"
    "*** Add File: src/b.py\n"
    "+created\n"
    "*** Delete File: src/c.py\n"
    "*** End Patch\n"
)


def write_apply_patch_rollout(path: Path, thread_id: str) -> Path:
    """Write a codex rollout carrying one apply_patch call touching src/{a,b,c}.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": "2026-07-16T16:44:00.000Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": "/tmp/demo", "originator": "codex_exec", "source": "exec"},
        },
        {
            "timestamp": "2026-07-16T16:44:00.500Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "patch it"},
        },
        {
            "timestamp": "2026-07-16T16:44:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": APPLY_PATCH_ENVELOPE,
                "call_id": "call-1",
            },
        },
    ]
    path.write_text("".join(f"{json.dumps(line)}\n" for line in lines))
    return path


def slot_path(session_id: str) -> Path:
    return (
        Path(os.environ["CAPTAIN_HOOK_STATE_DIR"]) / "hooks" / "sessions" / session_id / "registered_transcripts.json"
    )


def read_entries(session_id: str) -> list[dict[str, object]]:
    return json.loads(slot_path(session_id).read_text())["entries"]


class TestRegisterCommand:
    def test_register_writes_slot(self) -> None:
        result = CliRunner().invoke(
            cli, ["transcripts", "register", "--session", "s-cli", "--provider", "codex", "--thread-id", "thread-1"]
        )
        assert result.exit_code == 0, result.output
        (entry,) = read_entries("s-cli")
        assert entry["provider"] == "codex"
        assert entry["thread_id"] == "thread-1"
        assert entry["path"] is None

    def test_register_by_path_records_path(self, tmp_path: Path) -> None:
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("{}\n")
        result = CliRunner().invoke(cli, ["transcripts", "register", "--session", "s-path", "--path", str(rollout)])
        assert result.exit_code == 0, result.output
        (entry,) = read_entries("s-path")
        assert entry["path"] == str(rollout)
        assert entry["thread_id"] is None

    def test_register_is_idempotent(self) -> None:
        runner = CliRunner()
        args = ["transcripts", "register", "--session", "s-idem", "--thread-id", "thread-x"]
        assert runner.invoke(cli, args).exit_code == 0
        assert runner.invoke(cli, args).exit_code == 0
        assert len(read_entries("s-idem")) == 1

    def test_register_both_locators_is_usage_error(self) -> None:
        result = CliRunner().invoke(
            cli, ["transcripts", "register", "--session", "s-both", "--thread-id", "t", "--path", "/tmp/x.jsonl"]
        )
        assert result.exit_code == 2
        assert "exactly one" in result.output
        assert not slot_path("s-both").exists()

    def test_register_neither_locator_is_usage_error(self) -> None:
        result = CliRunner().invoke(cli, ["transcripts", "register", "--session", "s-none"])
        assert result.exit_code == 2
        assert "exactly one" in result.output

    @pytest.mark.parametrize("flag", ["--path", "--thread-id"])
    def test_register_empty_locator_is_usage_error(self, flag: str) -> None:
        result = CliRunner().invoke(cli, ["transcripts", "register", "--session", "s-empty", flag, ""])
        assert result.exit_code == 2
        assert "exactly one" in result.output
        assert not slot_path("s-empty").exists()

    @pytest.mark.parametrize("bad", ["../evil", "a/b", "..", ".", "a\x00b"])
    def test_register_rejects_traversal_session_id(self, bad: str) -> None:
        result = CliRunner().invoke(cli, ["transcripts", "register", "--session", bad, "--thread-id", "t"])
        assert result.exit_code == 2
        assert "invalid session id" in result.output


def test_warm_command_uses_registered_ids_and_only_prints_counters(tmp_path, monkeypatch):
    from captain_hook.snapshots.client import SnapshotClient

    register_transcript("s-warm", thread_id="thread-1")
    calls = []
    client = SnapshotClient(lambda _: {})
    client.bind_tool_registry({})

    class RootSession:
        def view(self):
            return {"attachments": []}

        def release(self):
            pass

    def warm(operation, **arguments):
        calls.append((operation, arguments))
        if operation == "warm_root":
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 1 if len(calls) == 1 else 0, "cache_hits": 0},
                "data": {
                    "kind": "warmed_root",
                    "owner_epoch": "owner",
                    "source_revision": "revision",
                    "source_offset": 100,
                    "source_size": 100,
                    "complete": True,
                    "facts_complete": True,
                },
            }
        return {
            "status": "ok",
            "usage": {
                "source_bytes_read": 1 if len(calls) == 3 else 0,
                "discovery_entries_examined": 0,
                "cache_hits": 0 if len(calls) == 3 else 1,
            },
            "data": {
                "kind": "warmed_registry",
                "owner_epoch": "owner",
                "membership_revision": "revision",
                "next_index": 1,
                "complete": True,
                "source_offset": 0,
                "source_size": 0,
                "fact_cache_bytes": 100,
                "fact_cache_write_bytes": 50,
                "fact_cache_writes": 1,
            },
        }

    @contextmanager
    def scope():
        yield client

    monkeypatch.setattr(client, "call", warm)
    monkeypatch.setattr(client, "acquire", lambda _, tail_bytes: RootSession())
    monkeypatch.setattr(
        client,
        "pages",
        lambda *args, **kwargs: iter(({"kind": "classifier", "classifier": {"id": "native", "version": "1"}},)),
    )
    monkeypatch.setattr(
        "captain_hook.transcripts.resolved_transcript_paths",
        lambda *_args, **_kwargs: {SessionId("s-warm"): tmp_path / "root.jsonl"},
    )
    monkeypatch.setattr("captain_hook.snapshots.client.client_scope", scope)
    monkeypatch.setattr("captain_hook.cli.CliState.discover", lambda self: [])
    result = CliRunner().invoke(cli, ["transcripts", "warm", "--session", "s-warm", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "complete": True,
        "root_steps": 1,
        "root_source_bytes_read": 1,
        "root_verify_source_bytes_read": 0,
        "root_facts_complete": True,
        "sources": 1,
        "steps": 1,
        "fact_cache_bytes": 100,
        "fact_cache_write_bytes": 50,
        "fact_cache_writes": 1,
        "warm_source_bytes_read": 1,
        "verify_complete": True,
        "verify_steps": 1,
        "verify_source_bytes_read": 0,
        "verify_cache_hits": 1,
        "elapsed_seconds": pytest.approx(0, abs=1),
        "status": "complete",
    }
    assert [operation for operation, _ in calls] == ["warm_root", "warm_root", "warm_registered", "warm_registered"]
    assert calls[2][1]["thread_ids"] == ["thread-1"]
    assert calls[2][1]["limits"]["max_source_read_bytes"] == 8 * 1024 * 1024


def test_warm_command_does_not_pace_cache_only_progress(tmp_path, monkeypatch):
    from captain_hook.snapshots.client import SnapshotClient

    register_transcript("s-cached", thread_id="thread-1")
    client = SnapshotClient(lambda _: {})
    client.bind_tool_registry({})
    calls = []

    class RootSession:
        def view(self):
            return {"attachments": []}

        def release(self):
            pass

    def warm(operation, **arguments):
        calls.append((operation, arguments))
        if operation == "warm_root":
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 0, "cache_hits": 1},
                "data": {
                    "kind": "warmed_root",
                    "owner_epoch": "owner",
                    "source_revision": "revision",
                    "source_offset": 100,
                    "source_size": 100,
                    "complete": True,
                    "facts_complete": True,
                },
            }
        registry_calls = sum(name == "warm_registered" for name, _ in calls)
        return {
            "status": "ok",
            "usage": {"source_bytes_read": 0, "discovery_entries_examined": 0, "cache_hits": 8},
            "data": {
                "kind": "warmed_registry",
                "owner_epoch": "owner",
                "membership_revision": "revision",
                "next_index": 8 if registry_calls % 2 else 10,
                "complete": registry_calls % 2 == 0,
                "source_offset": 0,
                "source_size": 0,
                "fact_cache_bytes": 100,
                "fact_cache_write_bytes": 0,
                "fact_cache_writes": 0,
            },
        }

    @contextmanager
    def scope():
        yield client

    monkeypatch.setattr(client, "call", warm)
    monkeypatch.setattr(client, "acquire", lambda _, tail_bytes: RootSession())
    monkeypatch.setattr(
        client,
        "pages",
        lambda *args, **kwargs: iter(({"kind": "classifier", "classifier": {"id": "native", "version": "1"}},)),
    )
    monkeypatch.setattr(
        "captain_hook.transcripts.resolved_transcript_paths",
        lambda *_args, **_kwargs: {SessionId("s-cached"): tmp_path / "root.jsonl"},
    )
    monkeypatch.setattr("captain_hook.snapshots.client.client_scope", scope)
    monkeypatch.setattr("captain_hook.cli.CliState.discover", lambda self: [])
    monkeypatch.setattr("captain_hook.worker.service.WARM_INTERVAL_SECONDS", 10)
    started = time.monotonic()
    result = CliRunner().invoke(cli, ["transcripts", "warm", "--session", "s-cached", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert time.monotonic() - started < 1
    assert len(calls) == 6
    assert json.loads(result.output)["verify_cache_hits"] == 16


def test_warm_command_prepares_root_classifier_without_registered_sources(tmp_path, monkeypatch):
    from captain_hook.snapshots.client import SnapshotClient

    client = SnapshotClient(lambda _: {})
    client.bind_tool_registry({})
    calls = []

    class RootSession:
        def view(self):
            return {"attachments": []}

        def release(self):
            pass

    def warm(operation, **arguments):
        calls.append((operation, arguments))
        return {
            "status": "ok",
            "usage": {"source_bytes_read": 0, "cache_hits": 1},
            "data": {
                "kind": "warmed_root",
                "owner_epoch": "owner",
                "source_revision": "revision",
                "source_offset": 100,
                "source_size": 100,
                "complete": True,
                "facts_complete": True,
            },
        }

    @contextmanager
    def scope():
        yield client

    monkeypatch.setattr(client, "call", warm)
    monkeypatch.setattr(client, "acquire", lambda _, tail_bytes: RootSession())
    monkeypatch.setattr(
        client,
        "pages",
        lambda *args, **kwargs: iter(({"kind": "classifier", "classifier": {"id": "captain-lane", "version": "1"}},)),
    )
    monkeypatch.setattr(
        "captain_hook.transcripts.resolved_transcript_paths",
        lambda *_args, **_kwargs: {SessionId("root-only"): tmp_path / "root.jsonl"},
    )
    monkeypatch.setattr("captain_hook.snapshots.client.client_scope", scope)
    monkeypatch.setattr("captain_hook.cli.CliState.discover", lambda self: [])
    result = CliRunner().invoke(cli, ["transcripts", "warm", "--session", "root-only", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert [arguments["classifier"]["id"] for _, arguments in calls] == [
        "native",
        "native",
        "captain-lane",
        "captain-lane",
    ]
    assert json.loads(result.output)["root_facts_complete"] is True
    assert json.loads(result.output)["sources"] == 0


def test_warm_command_reports_unavailable_configured_classifier(tmp_path, monkeypatch):
    from captain_hook.snapshots.client import SnapshotClient

    client = SnapshotClient(lambda _: {})
    client.bind_tool_registry({})
    calls = []

    def warm(operation, **arguments):
        calls.append((operation, arguments))
        return {
            "status": "ok",
            "usage": {"source_bytes_read": 0, "cache_hits": 1},
            "data": {
                "kind": "warmed_root",
                "owner_epoch": "owner",
                "source_revision": "revision",
                "source_offset": 100,
                "source_size": 100,
                "complete": True,
                "facts_complete": True,
            },
        }

    @contextmanager
    def scope():
        yield client

    monkeypatch.setattr(client, "call", warm)
    monkeypatch.setattr(client, "acquire", lambda _, tail_bytes: pytest.fail("configured callback ran in operator"))
    monkeypatch.setattr(
        "captain_hook.transcripts.resolved_transcript_paths",
        lambda *_args, **_kwargs: {SessionId("configured"): tmp_path / "root.jsonl"},
    )
    monkeypatch.setattr(
        "captain_hook.transcripts.configured_classifier_policy",
        lambda _: {"id": "captain-configured", "version": "digest"},
    )
    monkeypatch.setattr("captain_hook.snapshots.client.client_scope", scope)
    monkeypatch.setattr("captain_hook.cli.CliState.discover", lambda self: [])
    monkeypatch.setattr("captain_hook.cli._state.classifier", lambda _: True)
    result = CliRunner().invoke(cli, ["transcripts", "warm", "--session", "configured", "--root", str(tmp_path)])

    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "classifier_unavailable"
    assert json.loads(result.output)["root_facts_complete"] is False
    assert [arguments["classifier"]["id"] for _, arguments in calls] == ["native", "native"]


class TestMcpTool:
    def test_mcp_tool_hits_the_same_slot(self) -> None:
        from captain_hook.mcp_server import build_mcp_server

        server = build_mcp_server()
        asyncio.run(server.call_tool("register_transcript", {"session_id": "s-mcp", "thread_id": "thread-mcp"}))
        (entry,) = read_entries("s-mcp")
        assert entry["provider"] == "codex"
        assert entry["thread_id"] == "thread-mcp"


MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SESSION_TIMEOUT_S = 60.0


class McpStdioSession:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self._last_id = 0

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._last_id += 1
        self._send({"jsonrpc": "2.0", "id": self._last_id, "method": method, "params": params or {}})
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                assert self.proc.stderr is not None
                raise AssertionError(f"server closed stdout during {method}; stderr:\n{self.proc.stderr.read()}")
            message = json.loads(line)
            if message.get("id") == self._last_id:
                assert "error" not in message, message["error"]
                return message["result"]


@pytest.fixture
def mcp_session() -> Iterator[McpStdioSession]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "captain_hook", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    watchdog = threading.Timer(MCP_SESSION_TIMEOUT_S, proc.kill)
    watchdog.start()
    try:
        yield McpStdioSession(proc)
    finally:
        watchdog.cancel()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def initialized_mcp_session(mcp_session: McpStdioSession) -> Iterator[tuple[McpStdioSession, dict[str, Any]]]:
    result = mcp_session.request(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "capt-hook-tests", "version": "1"},
        },
    )
    mcp_session.notify("notifications/initialized")
    yield mcp_session, result


class TestMcpServerLiveness:
    """`capt-hook mcp` must actually start and speak MCP — registration in a manifest is not liveness."""

    def test_initialize_handshake_completes(
        self, initialized_mcp_session: tuple[McpStdioSession, dict[str, Any]]
    ) -> None:
        _, result = initialized_mcp_session
        assert result["serverInfo"]["name"] == "capt-hook"
        assert result["serverInfo"]["version"] == importlib.metadata.version("capt-hook")
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert "tools" in result["capabilities"]

    def test_tools_list_exposes_exactly_register_transcript(
        self, initialized_mcp_session: tuple[McpStdioSession, dict[str, Any]]
    ) -> None:
        session, _ = initialized_mcp_session
        (tool,) = session.request("tools/list")["tools"]
        assert tool["name"] == "register_transcript"
        assert tool["inputSchema"]["required"] == ["session_id"]
        assert set(tool["inputSchema"]["properties"]) == {"session_id", "provider", "thread_id", "path", "label"}

    def test_tools_call_registers_through_the_protocol(
        self, initialized_mcp_session: tuple[McpStdioSession, dict[str, Any]]
    ) -> None:
        session, _ = initialized_mcp_session
        result = session.request(
            "tools/call",
            {"name": "register_transcript", "arguments": {"session_id": "s-live", "thread_id": "thread-live"}},
        )
        assert result["isError"] is False
        (entry,) = read_entries("s-live")
        assert entry["provider"] == "codex"
        assert entry["thread_id"] == "thread-live"

    def test_server_stays_up_across_requests(
        self, initialized_mcp_session: tuple[McpStdioSession, dict[str, Any]]
    ) -> None:
        session, _ = initialized_mcp_session
        session.request("tools/list")
        session.request("ping")
        assert session.proc.poll() is None


@pytest.fixture
def discovery_client(monkeypatch):
    from captain_hook.snapshots.client import CURRENT_CLIENT
    from tests.snapshot_discovery_helpers import DiscoveryClient

    client = DiscoveryClient()
    token = CURRENT_CLIENT.set(client)
    monkeypatch.setattr("cc_transcript.codex.discover", lambda *args: pytest.fail("local discovery"))
    try:
        yield client
    finally:
        CURRENT_CLIENT.reset(token)


class TestRegisteredPaths:
    def test_preserves_registration_order_and_owner_canonical_paths(self, tmp_path, discovery_client):
        register_transcript("s-order", thread_id="b")
        register_transcript("s-order", path=str(tmp_path / "alias.jsonl"))
        register_transcript("s-order", thread_id="missing")
        register_transcript("s-order", thread_id="a")
        discovery_client.results = {"a": tmp_path / "a.jsonl", "b": tmp_path / "b.jsonl"}
        discovery_client.paths[str(tmp_path / "alias.jsonl")] = tmp_path / "canonical.jsonl"
        assert registered_paths(ensure_session(SessionId("s-order"))) == (
            tmp_path / "b.jsonl",
            tmp_path / "canonical.jsonl",
            tmp_path / "a.jsonl",
        )
        assert discovery_client.released == [str(tmp_path / "alias.jsonl")]

    def test_locates_many_ids_in_one_request_without_leases(self, tmp_path, discovery_client):
        ids = [f"session-{i}" for i in range(257)]
        for session_id in ids:
            register_transcript("s-many", thread_id=session_id)
        discovery_client.results = {session_id: tmp_path / f"{session_id}.jsonl" for session_id in ids}
        discovery_client.page_size = 17
        assert registered_paths(ensure_session(SessionId("s-many"))) == tuple(discovery_client.results.values())
        assert len(discovery_client.requests) == 1
        assert discovery_client.requests[0]["session_ids"] == ids
        assert discovery_client.released == []

    def test_deduplicates_owner_canonical_paths(self, tmp_path, discovery_client):
        register_transcript("s-aliases", thread_id="first")
        register_transcript("s-aliases", path=str(tmp_path / "alias.jsonl"))
        register_transcript("s-aliases", thread_id="second")
        canonical = tmp_path / "canonical.jsonl"
        discovery_client.results = {"first": canonical, "second": canonical}
        discovery_client.paths[str(tmp_path / "alias.jsonl")] = canonical

        assert registered_paths(ensure_session(SessionId("s-aliases"))) == (canonical,)
        assert discovery_client.released == [str(tmp_path / "alias.jsonl")]

    def test_batches_only_beyond_locate_request_bound(self, tmp_path, discovery_client):
        ids = [SessionId(f"session-{i}") for i in range(1025)]
        assert resolved_transcript_paths(discovery_client, ids, roots=[tmp_path]) == dict.fromkeys(ids)
        assert [len(request["session_ids"]) for request in discovery_client.requests] == [1024, 1]

    def test_owner_is_consulted_again_after_a_prior_missing_result(self, tmp_path, discovery_client):
        register_transcript("s-new", thread_id="late")
        directory = ensure_session(SessionId("s-new"))
        assert registered_paths(directory) == ()
        discovery_client.results["late"] = tmp_path / "late.jsonl"
        assert registered_paths(directory) == (tmp_path / "late.jsonl",)
        assert len(discovery_client.requests) == 2

    def test_relative_path_remains_anchored_to_registration_directory(self, tmp_path, monkeypatch, discovery_client):
        lane = tmp_path / "lane"
        lane.mkdir()
        monkeypatch.chdir(lane)
        register_transcript("s-relative", path="rollout.jsonl")
        target = lane / "rollout.jsonl"
        discovery_client.paths[str(target)] = target
        monkeypatch.chdir(tmp_path)
        assert registered_paths(ensure_session(SessionId("s-relative"))) == (target,)

    def test_empty_optional_path_does_not_hide_thread_locator(self, tmp_path, discovery_client):
        register_transcript("s-empty-path", thread_id="thread", path="")
        discovery_client.results["thread"] = tmp_path / "thread.jsonl"
        assert registered_paths(ensure_session(SessionId("s-empty-path"))) == (tmp_path / "thread.jsonl",)
        assert discovery_client.acquired == []

    def test_missing_direct_path_is_skipped(self, tmp_path, discovery_client):
        register_transcript("s-missing", path=str(tmp_path / "gone.jsonl"))
        assert registered_paths(ensure_session(SessionId("s-missing"))) == ()

    def test_incomplete_item_is_not_treated_as_missing(self, tmp_path, discovery_client):
        from captain_hook.snapshots.client import EvidenceIncomplete

        for session_id in ("incomplete", "healthy"):
            register_transcript("s-partial", thread_id=session_id)
        discovery_client.results = {"incomplete": "incomplete", "healthy": tmp_path / "healthy.jsonl"}
        with pytest.raises(EvidenceIncomplete, match="location did not complete"):
            registered_paths(ensure_session(SessionId("s-partial")))
        assert discovery_client.released == []

    def test_later_page_failure_discards_partial_locations(self, tmp_path, discovery_client):
        from captain_hook.snapshots.client import EvidenceIncomplete

        register_transcript("s-later-page", thread_id="healthy")
        discovery_client.results["healthy"] = tmp_path / "healthy.jsonl"
        discovery_client.failure_after_page = EvidenceIncomplete("deadline", "fixture timeout")
        with pytest.raises(EvidenceIncomplete, match="deadline"):
            registered_paths(ensure_session(SessionId("s-later-page")))
        assert discovery_client.released == []

    def test_incomplete_after_missing_page_is_not_treated_as_missing(self, discovery_client):
        from captain_hook.snapshots.client import EvidenceIncomplete

        register_transcript("s-not-missing", thread_id="unknown")
        discovery_client.failure_after_page = EvidenceIncomplete("incomplete", "unfinished lookup")
        with pytest.raises(EvidenceIncomplete, match="unfinished lookup"):
            registered_paths(ensure_session(SessionId("s-not-missing")))

    def test_missing_requested_verdict_is_not_absence(self, discovery_client, monkeypatch):
        from captain_hook.snapshots.client import SnapshotProtocolError

        register_transcript("s-omitted", thread_id="unknown")
        monkeypatch.setattr(
            discovery_client, "pages", lambda *args, **kwargs: iter([{"kind": "located", "sessions": []}])
        )
        with pytest.raises(SnapshotProtocolError, match="omitted"):
            registered_paths(ensure_session(SessionId("s-omitted")))

    def test_duplicate_across_pages_is_rejected(self, tmp_path, discovery_client, monkeypatch):
        from captain_hook.snapshots.client import SnapshotProtocolError

        register_transcript("s-duplicate", thread_id="a")
        session = {"session_id": "a", "status": "ok", "path": str(tmp_path / "a.jsonl"), "revision": "1:2:3:4:5"}
        monkeypatch.setattr(
            discovery_client, "pages", lambda *args, **kwargs: iter([{"kind": "located", "sessions": [session]}] * 2)
        )
        with pytest.raises(SnapshotProtocolError, match="duplicate"):
            registered_paths(ensure_session(SessionId("s-duplicate")))


class TestLocatorValidation:
    @pytest.mark.parametrize("kwargs", [{"path": ""}, {"thread_id": ""}, {"path": "", "thread_id": ""}])
    def test_empty_locator_is_rejected_without_writing_slot(self, kwargs: dict[str, str]) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            register_transcript("s-empty-direct", provider="codex", **kwargs)
        assert not slot_path("s-empty-direct").exists()


class TestUnsafePathsSkipped:
    @pytest.mark.parametrize("status", ["source_limit", "parse_error", "invalid_request", "retained_limit"])
    def test_direct_path_errors_are_not_silently_omitted(self, tmp_path, discovery_client, status):
        from captain_hook.snapshots.client import EvidenceIncomplete

        path = str(tmp_path / "source.jsonl")
        register_transcript("s-error", path=path)
        discovery_client.paths[path] = EvidenceIncomplete(status, "fixture")
        with pytest.raises(EvidenceIncomplete) as raised:
            registered_paths(ensure_session(SessionId("s-error")))
        assert raised.value.status == status


@pytest.mark.parametrize(
    "event", [Event.SessionStart, Event.UserPromptSubmit, Event.PreToolUse, Event.PostToolUse, Event.Stop]
)
def test_codex_hook_dispatch_reads_native_root_transcript(tmp_path, event):
    from captain_hook.snapshots.client import CURRENT_CLIENT
    from captain_hook.testing.snapshots import FixtureOwner
    from captain_hook.util import reqenv

    rollout = write_apply_patch_rollout(tmp_path / "rollout.jsonl", "codex-root")
    observed = []

    @on(event)
    def root_evidence(evt):
        observed.append(evt.ctx.t.has_edit_to("src/a.py", subagents=False))

    raw = {
        "session_id": "codex-root",
        "transcript_path": str(rollout),
        "cwd": str(tmp_path),
        "hook_event_name": event.name,
        "model": "gpt-6.1-sol",
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
        "tool_response": "/tmp/demo\n",
    }
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    scope = reqenv.RequestOverrides({"CAPT_HOOK_PROVIDER": "codex"}, str(tmp_path), 0, "codex-root")
    try:
        with reqenv.use_request(scope):
            dispatch_event(tmp_path, event, raw, session_dir=ensure_session(SessionId("codex-root")))
        assert observed == [True]
    finally:
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_unknown_hook_provider_is_rejected_before_dispatch(tmp_path):
    from captain_hook.util import reqenv

    scope = reqenv.RequestOverrides({"CAPT_HOOK_PROVIDER": "unknown"}, str(tmp_path), 0, "unknown-provider")
    with reqenv.use_request(scope):
        with pytest.raises(ValueError, match="unsupported hook provider"):
            dispatch_event(tmp_path, Event.SessionStart, {}, session_dir=None)


def test_dispatch_folds_registered_rollout_into_deep_gate(tmp_path):
    from captain_hook.snapshots.client import CURRENT_CLIENT
    from captain_hook.testing.helpers import fixture_line
    from captain_hook.testing.snapshots import FixtureOwner

    rollout = write_apply_patch_rollout(tmp_path / "rollout.jsonl", "thread-e2e")
    register_transcript("s-e2e", provider="codex", path=str(rollout))
    main = tmp_path / "main.jsonl"
    main.write_text(
        "".join(
            json.dumps(fixture_line(index, message)) + "\n"
            for index, message in enumerate(
                (
                    raw_text("user", "look at the code"),
                    raw_assistant(raw_text_block("reading"), raw_tool_use("Read", {"file_path": "src/main.py"}, "tu1")),
                )
            )
        )
    )
    fired: list[str] = []

    @on(Event.Stop)
    def deep_gate(evt):
        if evt.ctx.t.has_edit_to("*", subagents=True):
            fired.append("deep")

    @on(Event.Stop)
    def bare_gate(evt):
        if evt.ctx.t.has_edit_to("*", subagents=False):
            fired.append("bare")

    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    try:
        dispatch_event(
            tmp_path,
            Event.Stop,
            {"session_id": "s-e2e", "transcript_path": str(main)},
            session_dir=ensure_session(SessionId("s-e2e")),
        )
        assert fired == ["deep"]
        assert fixture.client.call("stats")["data"]["counters"]["cold_parses"] == 2
    finally:
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_dispatch_reads_registered_sources_once_per_phase(tmp_path, monkeypatch):
    from captain_hook.snapshots.client import Lease, RemoteSession
    from captain_hook.transcripts import registered_sources, release_transcript
    from tests.test_prepared_graph_evidence import RecordingClient, description

    register_transcript("s-both", thread_id="first")
    register_transcript("s-both", thread_id="second")
    seen = []
    reads = []
    loads = []
    client = RecordingClient()
    source = description()

    def sources(session_dir):
        reads.append(session_dir)
        return registered_sources(session_dir)

    def loader(_):
        loads.append(True)
        return RemoteSession(
            client,
            Lease(client, source),
            Path(source["canonical_path"]),
            source["classifier"],
        )

    def observe(_event, evt, session_dir, advisory=True):
        seen.append(evt.ctx.t.graph.sources.thread_ids)
        release_transcript(evt.ctx.transcript)
        return None

    monkeypatch.setattr("captain_hook.transcripts.registered_sources", sources)
    monkeypatch.setattr("captain_hook.cli.dispatch", observe)
    monkeypatch.setattr(
        "captain_hook.cli.after_reply", lambda event, evt, raw, session_dir: observe(event, evt, session_dir)
    )
    session_dir = ensure_session(SessionId("s-both"))
    for index in range(2):
        if index:
            register_transcript("s-both", thread_id="third")
        _, background = dispatch_event(
            tmp_path,
            Event.Stop,
            {"session_id": "s-both", "transcript_path": str(tmp_path / "main.jsonl")},
            session_dir=session_dir,
            transcript_loader=loader,
        )
        background()

    assert seen == [
        ("first", "second"),
        ("first", "second"),
        ("first", "second", "third"),
        ("first", "second", "third"),
    ]
    assert len(reads) == 4
    assert len(loads) == 4


def test_each_hook_reads_only_the_tail_it_declares(tmp_path, monkeypatch):
    from cc_transcript.query import Session

    tails = []
    loads = []
    seen = {}

    @on(Event.Stop, transcript_events=30)
    def recent_gate(evt):
        seen["recent"] = len(evt.ctx.t)

    @on(Event.Stop, transcript_events=30)
    def other_recent_gate(evt):
        seen["other"] = len(evt.ctx.t)

    @on(Event.Stop)
    def history_gate(evt):
        seen["history"] = len(evt.ctx.t)

    def tail(path, count):
        tails.append((path, count))
        return Session(())

    def load(path):
        loads.append(path)
        return Session(())

    monkeypatch.setattr("captain_hook.transcripts.tail_transcript", tail)
    dispatch_event(
        tmp_path,
        Event.Stop,
        {"session_id": "s-window", "transcript_path": str(tmp_path / "main.jsonl")},
        session_dir=ensure_session(SessionId("s-window")),
        transcript_loader=load,
    )

    assert seen == {"recent": 0, "other": 0, "history": 0}
    assert tails == [(str(tmp_path / "main.jsonl"), 30)]
    assert loads == [str(tmp_path / "main.jsonl")]


FIRST_SESSION = "s-first-prompt"
FIRST_PROMPT = "1. add foo\n2. fix bar\n3. update baz"
TASKS_NUDGE = "general.tasks:nudge_7ef627f7"
TASKS_WARNING = {
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": (
            "This message has several distinct requests. Run `TaskCreate` for each before starting work."
        ),
    }
}
OPTION_DUMP = "Option 1 — refactor now\nOption 2 — defer it\nlet me know which you'd prefer"
QUEUED_WORDS = "ship it once the build is green"
UNBORN_REASON = "missing: No such file or directory (os error 2)"
SHOW_SIGNALS = Signals(
    [
        Signal(pattern=r"(?im)^\s*(?:\*\*)?(?:option|approach|alternative|path)\s*[A-D1-4]\b", weight=2),
        Signal(pattern=r"(?im)^\s*\d+[.)]\s.+\n(?:.*\n){0,2}\s*\d+[.)]\s", weight=1),
        Signal(pattern=r"(?i)\blet me know (?:which|what you think|if (?:this|that) (?:works|looks))\b", weight=2),
        Signal(pattern=r"(?i)\b(?:approve|sign[- ]?off|pick one|choose (?:one|between))\b", weight=1),
        Signal(
            pattern=r"(?i)\b(?:open|view) (?:it|the (?:report|page|file)) (?:at|in)\b"
            r"|\bsaved (?:the )?(?:report|summary|review) to\b",
            weight=2,
        ),
    ],
    threshold=3,
    window="turn",
    scope="window",
)


class ModelUnavailable(Exception):
    pass


def first_raw(root: Path, event: Event, **fields: Any) -> dict[str, Any]:
    return {
        "session_id": FIRST_SESSION,
        "transcript_path": str(first_root(root)),
        "cwd": str(root),
        "hook_event_name": event.name,
        "permission_mode": "default",
    } | fields


def first_root(root: Path) -> Path:
    return root / "projects" / f"{FIRST_SESSION}.jsonl"


def first_session_dir() -> Path:
    return ensure_session(SessionId(FIRST_SESSION))


def unborn_allowance() -> UnbornTranscript | None:
    return SessionSlot(first_session_dir(), UnbornTranscript).get()


@contextmanager
def first_request(root: Path, env: dict[str, str] | None = None) -> Iterator[Any]:
    from captain_hook.util import reqenv

    request = reqenv.RequestOverrides(
        env={key: os.environ[key] for key in ("CAPTAIN_HOOK_STATE_DIR", "CAPT_HOOK_DECISIONS_DB")} | (env or {}),
        cwd=str(root),
        client_ppid=os.getpid(),
        session_id=FIRST_SESSION,
    )
    with reqenv.use_request(request):
        yield request


def start_session(root: Path, source: str, *, env: dict[str, str] | None = None) -> None:
    with first_request(root, env):
        dispatch_event(
            root,
            Event.SessionStart,
            first_raw(root, Event.SessionStart, source=source),
            session_dir=first_session_dir(),
        )


def sync_event(root: Path, event: Event, **fields: Any) -> list[str]:
    with first_request(root) as request:
        dispatch_event(root, event, first_raw(root, event, **fields), session_dir=first_session_dir())
    return request.evidence_gaps


def submit_prompt(
    root: Path,
    prompt: str,
    *,
    env: dict[str, str] | None = None,
    transcript_loader: Any = None,
    **fields: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    with first_request(root, env) as request:
        envelope, background = dispatch_event(
            root,
            Event.UserPromptSubmit,
            first_raw(root, Event.UserPromptSubmit, prompt=prompt, **fields),
            session_dir=first_session_dir(),
            transcript_loader=transcript_loader,
        )
        background()
    return envelope, request.evidence_gaps


def write_transcript(path: Path, *messages: dict[str, Any]) -> Path:
    from captain_hook.testing.helpers import fixture_line

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(fixture_line(index, message)) + "\n" for index, message in enumerate(messages)))
    return path


@pytest.fixture
def snapshot_owner() -> Iterator[Any]:
    from captain_hook.snapshots.client import CURRENT_CLIENT
    from captain_hook.testing.snapshots import FixtureOwner

    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    try:
        yield fixture
    finally:
        CURRENT_CLIENT.reset(token)
        fixture.close()


@pytest.fixture
def model_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    def unavailable(self, template, *args, **kwargs):
        calls.append(template.system_text)
        raise ModelUnavailable("tests keep the model boundary closed")

    monkeypatch.setattr("captain_hook.context.HookContext.call_llm", unavailable)
    return calls


@pytest.fixture
def first_prompt_hooks(isolate_modules, model_calls) -> None:
    import captain_hook
    from captain_hook import FromSubagent, llm_nudge
    from captain_hook.app import _state
    from captain_hook.loader import discover_pack

    discover_pack("general", Path(captain_hook.__file__).parent / "builtin_packs" / "general" / "hooks")
    _state.hooks[:] = [hook for hook in _state.hooks if hook.name in {TASKS_NUDGE, "record_queued_words"}]
    llm_nudge(
        "Decide whether the deliverable wanted a surface.",
        message="This deliverable wanted a surface, not a wall of text.",
        events=Event.UserPromptSubmit | Event.PostToolUse,
        skip_if=[FromSubagent()],
        max_fires=2,
        signals=SHOW_SIGNALS,
    )


@pytest.fixture
def probe() -> list[int]:
    seen: list[int] = []

    @on(Event.UserPromptSubmit | Event.Stop | Event.PostToolUse)
    def probe(evt):
        seen.append(len(evt.ctx.t))

    return seen


@pytest.mark.parametrize(
    ("prompt", "envelope", "fired"),
    [
        pytest.param(FIRST_PROMPT, TASKS_WARNING, (0, 0, ECHO_WINDOW, 0, [TASKS_NUDGE], True), id="matching"),
        pytest.param("just fix the typo", None, None, id="nonmatching"),
    ],
)
def test_first_prompt_reads_unborn_root_transcript_as_empty_history(
    tmp_path, first_prompt_hooks, model_calls, snapshot_owner, prompt, envelope, fired
):
    from captain_hook.grants import store
    from captain_hook.state import PrimitiveState

    start_session(tmp_path, "startup")
    assert unborn_allowance() == UnbornTranscript(path=str(first_root(tmp_path)))

    assert submit_prompt(tmp_path, prompt) == (envelope, [])

    state = SessionSlot(first_session_dir(), PrimitiveState).get()
    assert (
        state
        and (
            state.last_fired_at,
            state.last_fired_window,
            state.echo_window_end,
            state.echo_window_base,
            sorted(state.consumed),
            bool(state.echo_lemmas),
        )
    ) == fired
    assert not first_root(tmp_path).exists()
    assert unborn_allowance() == UnbornTranscript()
    assert model_calls == []
    assert store.grants("words", FIRST_SESSION) == []


@pytest.mark.parametrize(
    ("source", "allowance"),
    [pytest.param("startup", UnbornTranscript(), id="startup"), pytest.param("resume", None, id="resume")],
)
def test_first_prompt_keeps_a_written_root_transcripts_history(
    tmp_path, first_prompt_hooks, model_calls, snapshot_owner, source, allowance
):
    from captain_hook.grants import store

    queued = {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": QUEUED_WORDS,
            "commandMode": "prompt",
            "origin": {"kind": "human"},
        },
    }
    history: list[int] = []

    @on(Event.UserPromptSubmit)
    def written_history(evt):
        history.append(len(evt.ctx.t))

    start_session(tmp_path, source)
    write_transcript(first_root(tmp_path), T.user("compare the caching strategies"), T.assistant(OPTION_DUMP), queued)

    assert submit_prompt(tmp_path, "which one?") == (None, [])

    assert history == [3]
    assert model_calls
    assert all("Option 1 — refactor now" in call for call in model_calls)
    assert [item.quote for grant in store.grants("words", FIRST_SESSION) for item in grant.evidence] == [QUEUED_WORDS]
    assert unborn_allowance() == allowance
    assert submit_prompt(tmp_path, FIRST_PROMPT) == (TASKS_WARNING, [])
    assert history == [3, 3]


@pytest.mark.parametrize("transcript_events", [None, 30], ids=["full", "tail"])
@pytest.mark.parametrize(
    ("messages", "first", "second", "second_gaps"),
    [
        pytest.param(
            (),
            [0, 0],
            [],
            [f"probe: {UNBORN_REASON}", f"background_probe: {UNBORN_REASON}"],
            id="unborn-through-both-phases",
        ),
        pytest.param((T.user("first ask"), T.assistant("on it")), [0, 2], [2, 2], [], id="written-between-phases"),
    ],
)
def test_unborn_root_spans_only_its_own_event_phases(
    tmp_path, snapshot_owner, transcript_events, messages, first, second, second_gaps
):
    seen: list[int] = []

    @on(Event.UserPromptSubmit, transcript_events=transcript_events)
    def probe(evt):
        seen.append(len(evt.ctx.t))

    @on(Event.UserPromptSubmit, async_=True, transcript_events=transcript_events)
    def background_probe(evt):
        seen.append(len(evt.ctx.t))

    start_session(tmp_path, "startup")
    with first_request(tmp_path) as request:
        _, background = dispatch_event(
            tmp_path,
            Event.UserPromptSubmit,
            first_raw(tmp_path, Event.UserPromptSubmit, prompt="first ask"),
            session_dir=first_session_dir(),
        )
        if messages:
            write_transcript(first_root(tmp_path), *messages)
        background()
    assert (seen, request.evidence_gaps) == (first, [])

    seen.clear()
    assert submit_prompt(tmp_path, "second ask") == (None, second_gaps)
    assert seen == second


@pytest.mark.parametrize(
    ("source", "born_at_start", "env"),
    [
        pytest.param(None, False, {}, id="no-session-start"),
        pytest.param("resume", False, {}, id="resume"),
        pytest.param("compact", False, {}, id="compact"),
        pytest.param("startup", True, {}, id="written-at-startup"),
        pytest.param("startup", False, {"CAPT_HOOK_PROVIDER": "codex"}, id="codex"),
    ],
)
def test_missing_root_without_an_unborn_allowance_still_fails_open(
    tmp_path, probe, snapshot_owner, source, born_at_start, env
):
    if born_at_start:
        write_transcript(first_root(tmp_path), T.user("earlier"))
    if source:
        start_session(tmp_path, source, env=env)
    first_root(tmp_path).unlink(missing_ok=True)

    assert submit_prompt(tmp_path, "first ask", env=env) == (None, [f"probe: {UNBORN_REASON}"])
    assert probe == []
    assert unborn_allowance() is None


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(lambda root: {"agent_id": "agent-1"}, id="lane"),
        pytest.param(lambda root: {"transcript_path": str(root / "elsewhere.jsonl")}, id="other-path"),
    ],
)
def test_unborn_allowance_binds_the_root_prompt_of_its_session(tmp_path, probe, snapshot_owner, fields):
    start_session(tmp_path, "startup")

    assert submit_prompt(tmp_path, "teammate ask", **fields(tmp_path)) == (None, [f"probe: {UNBORN_REASON}"])
    assert unborn_allowance() == UnbornTranscript(path=str(first_root(tmp_path)))
    assert submit_prompt(tmp_path, "first ask") == (None, [])
    assert probe == [0]


def test_unborn_allowance_is_spent_by_the_first_prompt_alone(tmp_path, probe, snapshot_owner):
    start_session(tmp_path, "startup")

    assert sync_event(tmp_path, Event.Stop) == [f"probe: {UNBORN_REASON}"]
    assert sync_event(tmp_path, Event.PostToolUse, tool_name="Bash", tool_input={"command": "ls"}) == [
        f"probe: {UNBORN_REASON}"
    ]
    assert submit_prompt(tmp_path, "first ask") == (None, [])
    assert submit_prompt(tmp_path, "second ask") == (None, [f"probe: {UNBORN_REASON}"])
    assert probe == [0]
    assert unborn_allowance() == UnbornTranscript()


def test_unborn_root_keeps_failing_for_a_session_with_registered_transcripts(tmp_path, probe, snapshot_owner):
    start_session(tmp_path, "startup")
    register_transcript(
        FIRST_SESSION, provider="codex", path=str(write_apply_patch_rollout(tmp_path / "r.jsonl", "thread-first"))
    )

    assert submit_prompt(tmp_path, "first ask") == (None, [f"probe: {UNBORN_REASON}"])
    assert probe == []
    assert unborn_allowance() == UnbornTranscript()


@pytest.mark.parametrize(
    ("corrupt", "raised"),
    [
        pytest.param(lambda ledger: ledger.write_text("not valid json {{{"), ValidationError, id="malformed"),
        pytest.param(
            lambda ledger: ledger.write_text('{"entries": [{"provider": "codex"}]}'),
            ValidationError,
            id="invalid-entry",
        ),
        pytest.param(lambda ledger: ledger.mkdir(), IsADirectoryError, id="unreadable"),
    ],
)
def test_unborn_root_raises_on_an_unreadable_registered_ledger(tmp_path, probe, snapshot_owner, corrupt, raised):
    start_session(tmp_path, "startup")
    corrupt(SessionSlot(first_session_dir(), RegisteredTranscripts).path)

    with pytest.raises(raised):
        submit_prompt(tmp_path, "first ask")
    assert probe == []
    assert unborn_allowance() == UnbornTranscript()


def test_unborn_root_reads_an_absent_registered_ledger_as_no_registrations(tmp_path, probe, snapshot_owner):
    start_session(tmp_path, "startup")
    assert not SessionSlot(first_session_dir(), RegisteredTranscripts).path.exists()

    assert submit_prompt(tmp_path, "first ask") == (None, [])
    assert probe == [0]
    assert unborn_allowance() == UnbornTranscript()


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        pytest.param("deadline", "foreground transcript deadline exhausted", id="deadline"),
        pytest.param("changed", "Not a directory (os error 20)", id="changed"),
    ],
)
def test_unborn_root_keeps_other_fail_open_statuses(tmp_path, probe, status, reason):
    def incomplete(path):
        raise EvidenceIncomplete(status, reason)

    start_session(tmp_path, "startup")

    assert submit_prompt(tmp_path, "first ask", transcript_loader=incomplete) == (None, [f"probe: {status}: {reason}"])
    assert probe == []
    assert unborn_allowance() == UnbornTranscript()


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        pytest.param(
            EvidenceIncomplete("permission_denied", "Permission denied (os error 13)"),
            EvidenceIncomplete,
            id="permission",
        ),
        pytest.param(EvidenceIncomplete("parse_error", 'Key("timestamp")'), EvidenceIncomplete, id="parse-error"),
        pytest.param(
            SnapshotProtocolError("snapshot frame exceeds encoded byte bound"), SnapshotProtocolError, id="protocol"
        ),
        pytest.param(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), TranscriptLoadError, id="decode"
        ),
    ],
)
def test_unborn_root_still_raises_visible_evidence_errors(tmp_path, probe, error, raised):
    def failing(path):
        raise error

    start_session(tmp_path, "startup")

    with pytest.raises(raised) as caught:
        submit_prompt(tmp_path, "first ask", transcript_loader=failing)
    assert type(caught.value) is raised
    assert probe == []
    assert unborn_allowance() == UnbornTranscript()


def test_unborn_root_reads_a_malformed_written_file_as_an_error(tmp_path, probe, snapshot_owner):
    start_session(tmp_path, "startup")
    first_root(tmp_path).parent.mkdir(parents=True)
    first_root(tmp_path).write_text(json.dumps({"type": "user", "message": {"content": "no timestamp"}}) + "\n")

    with pytest.raises(EvidenceIncomplete) as caught:
        submit_prompt(tmp_path, "first ask")
    assert caught.value.status == "parse_error"
    assert probe == []


def test_unborn_allowance_is_claimed_once_across_threads_and_processes(tmp_path):
    session_dir = ensure_session(SessionId("s-claim"))
    path = str(tmp_path / "absent.jsonl")
    record_unborn(session_dir, path)
    script = (
        "import sys; from pathlib import Path; from captain_hook.transcripts import claim_unborn; "
        "print(claim_unborn(Path(sys.argv[1]), sys.argv[2]))"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(session_dir), path], stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    with ThreadPoolExecutor(4) as pool:
        threads = list(pool.map(lambda _: claim_unborn(session_dir, path), range(4)))
    children = [process.communicate(timeout=60)[0].strip() for process in processes]

    assert set(children) <= {"True", "False"}
    assert sorted([*threads, *(child == "True" for child in children)]) == [False] * 7 + [True]
    assert SessionSlot(session_dir, UnbornTranscript).get() == UnbornTranscript()


@pytest.mark.parametrize("transcript_events", [None, 30], ids=["full", "tail"])
def test_a_root_seen_written_never_reads_as_unborn_again(tmp_path, snapshot_owner, transcript_events):
    seen: list[int] = []

    @on(Event.UserPromptSubmit, transcript_events=transcript_events)
    def probe(evt):
        seen.append(len(evt.ctx.t))

    @on(Event.UserPromptSubmit, async_=True, transcript_events=transcript_events)
    def background_probe(evt):
        seen.append(len(evt.ctx.t))

    start_session(tmp_path, "startup")
    write_transcript(first_root(tmp_path), T.user("first ask"), T.assistant("on it"))
    with first_request(tmp_path) as request:
        _, background = dispatch_event(
            tmp_path,
            Event.UserPromptSubmit,
            first_raw(tmp_path, Event.UserPromptSubmit, prompt="first ask"),
            session_dir=first_session_dir(),
        )
        first_root(tmp_path).unlink()
        background()

    assert (seen, request.evidence_gaps) == ([2], [f"background_probe: {UNBORN_REASON}"])
    assert unborn_allowance() == UnbornTranscript()


@pytest.mark.parametrize(
    "content",
    [pytest.param("not valid json {{{", id="malformed"), pytest.param('{"path": 3}', id="wrong-type")],
)
def test_a_corrupt_unborn_allowance_raises_instead_of_granting(tmp_path, probe, snapshot_owner, content):
    start_session(tmp_path, "startup")
    SessionSlot(first_session_dir(), UnbornTranscript).path.write_text(content)

    with pytest.raises(ValidationError):
        submit_prompt(tmp_path, "first ask")
    assert probe == []


def test_a_failed_allowance_write_fails_the_startup(tmp_path, monkeypatch):
    def unwritable(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("captain_hook.session.atomic_write", unwritable)

    with pytest.raises(OSError, match="No space left on device"):
        start_session(tmp_path, "startup")
    assert unborn_allowance() is None


STEERING_GATE = "steering.steering:llm_gate_ff3aaa43"


@pytest.fixture
def steering_gate(isolate_modules, model_calls) -> None:
    import captain_hook
    from captain_hook.app import _state
    from captain_hook.loader import discover_pack

    discover_pack("steering", Path(captain_hook.__file__).parent / "builtin_packs" / "steering" / "hooks")
    _state.hooks[:] = [hook for hook in _state.hooks if hook.name == STEERING_GATE]


@pytest.mark.parametrize(
    ("event", "fields", "skipped"),
    [
        pytest.param(Event.SubagentStop, {"agent_type": ""}, True, id="subagent-stop-explicit-empty"),
        pytest.param(Event.SubagentStop, {}, False, id="subagent-stop-absent"),
        pytest.param(Event.SubagentStop, {"agent_type": "general-purpose"}, False, id="subagent-stop-typed"),
        pytest.param(Event.Stop, {"agent_type": ""}, False, id="stop-explicit-empty"),
        pytest.param(
            Event.PostToolUse,
            {"agent_type": "", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            False,
            id="post-tool-use-explicit-empty",
        ),
    ],
)
def test_steering_gate_skips_an_untyped_subagent_stop_before_any_transcript_read(
    tmp_path, steering_gate, model_calls, snapshot_owner, event, fields, skipped
):
    from captain_hook.app import _state
    from captain_hook.transcripts import load_transcript
    from captain_hook.types import InPlanMode, Waiting

    loads: list[str] = []

    def load(path):
        loads.append(str(path))
        return load_transcript(path)

    lane = tmp_path / "projects" / FIRST_SESSION / "subagents" / "agent-lane.jsonl"
    stop = {"agent_id": "agent-lane", "agent_transcript_path": str(lane)} if event is Event.SubagentStop else {}
    source = lane if event is Event.SubagentStop else first_root(tmp_path)
    with first_request(tmp_path) as request:
        envelope, _ = dispatch_event(
            tmp_path,
            event,
            first_raw(tmp_path, event, **stop, **fields),
            session_dir=first_session_dir(),
            transcript_loader=load,
        )

    (gate,) = _state.hooks
    assert (gate.name, gate.spec.skip_if) == (STEERING_GATE, (Waiting(), InPlanMode()))
    assert [type(condition.condition).__name__ for condition in gate.spec.only_if] == ["UntypedSubagentStop"]
    assert (envelope, model_calls) == (None, [])
    assert (loads, request.evidence_gaps) == (
        ([], []) if skipped else ([str(source)], [f"{STEERING_GATE}: {UNBORN_REASON}"])
    )
