from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cc_transcript.ids import SessionId
from click.testing import CliRunner

from captain_hook.app import on
from captain_hook.cli import cli, dispatch_event
from captain_hook.session import ensure_session
from captain_hook.transcripts import register_transcript, registered_paths
from captain_hook.types import Event
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
        assert set(discovery_client.released) == {"a", "b", str(tmp_path / "alias.jsonl")}

    def test_batches_ids_and_consumes_paginated_resolutions(self, tmp_path, discovery_client, monkeypatch):
        ids = [f"session-{i}" for i in range(257)]
        for session_id in ids:
            register_transcript("s-many", thread_id=session_id)
        discovery_client.results = {session_id: tmp_path / f"{session_id}.jsonl" for session_id in ids}
        discovery_client.page_size = 17
        live_leases = []
        release = discovery_client.call

        def track_release(operation, **arguments):
            live_leases.append(len(discovery_client._leases))
            return release(operation, **arguments)

        monkeypatch.setattr(discovery_client, "call", track_release)
        assert registered_paths(ensure_session(SessionId("s-many"))) == tuple(discovery_client.results.values())
        assert [len(request["session_ids"]) for request in discovery_client.requests] == [16] * 16 + [1]
        assert max(live_leases) == 16
        assert discovery_client._leases == set()
        assert set(discovery_client.released) == set(ids)

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

    def test_incomplete_item_releases_all_sibling_handles(self, tmp_path, discovery_client):
        from captain_hook.snapshots.client import EvidenceIncomplete

        for session_id in ("incomplete", "healthy"):
            register_transcript("s-partial", thread_id=session_id)
        discovery_client.results = {"incomplete": "incomplete", "healthy": tmp_path / "healthy.jsonl"}
        with pytest.raises(EvidenceIncomplete, match="resolution did not complete"):
            registered_paths(ensure_session(SessionId("s-partial")))
        assert discovery_client.released == ["healthy"]

    def test_later_page_failure_releases_earlier_handles(self, tmp_path, discovery_client):
        from captain_hook.snapshots.client import EvidenceIncomplete

        register_transcript("s-later-page", thread_id="healthy")
        discovery_client.results["healthy"] = tmp_path / "healthy.jsonl"
        discovery_client.failure_after_page = EvidenceIncomplete("deadline", "fixture timeout")
        with pytest.raises(EvidenceIncomplete, match="deadline"):
            registered_paths(ensure_session(SessionId("s-later-page")))
        assert discovery_client.released == ["healthy"]

    def test_incomplete_after_missing_page_is_not_treated_as_missing(self, discovery_client):
        from captain_hook.snapshots.client import EvidenceIncomplete

        register_transcript("s-not-missing", thread_id="unknown")
        discovery_client.failure_after_page = EvidenceIncomplete("incomplete", "unfinished lookup")
        with pytest.raises(EvidenceIncomplete, match="unfinished lookup"):
            registered_paths(ensure_session(SessionId("s-not-missing")))

    def test_missing_requested_verdict_is_not_absence(self, discovery_client, monkeypatch):
        from captain_hook.snapshots.client import SnapshotProtocolError

        register_transcript("s-omitted", thread_id="unknown")
        monkeypatch.setattr(discovery_client, "pages", lambda *args, **kwargs: iter([{"sessions": []}]))
        with pytest.raises(SnapshotProtocolError, match="omitted"):
            registered_paths(ensure_session(SessionId("s-omitted")))

    def test_cleanup_attempts_every_lease_when_release_fails(self, tmp_path, discovery_client, monkeypatch):
        from captain_hook.snapshots.client import EvidenceIncomplete

        for session_id in ("a", "b"):
            register_transcript("s-release", thread_id=session_id)
            discovery_client.results[session_id] = tmp_path / f"{session_id}.jsonl"
        release = discovery_client.call

        def fail_one(operation, **arguments):
            result = release(operation, **arguments)
            if arguments["token"] == "b":
                raise EvidenceIncomplete("deadline", "release fixture")
            return result

        monkeypatch.setattr(discovery_client, "call", fail_one)
        with pytest.raises(EvidenceIncomplete, match="release fixture"):
            registered_paths(ensure_session(SessionId("s-release")))
        assert discovery_client.released == ["a", "b", "b"]


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


def test_dispatch_reuses_registered_paths_between_sync_and_background(tmp_path, discovery_client, monkeypatch):
    from cc_transcript.query import Session

    from captain_hook.transcripts import release_transcript

    register_transcript("s-both", thread_id="first")
    register_transcript("s-both", thread_id="second")
    discovery_client.results = {
        "first": tmp_path / "first.jsonl",
        "second": tmp_path / "second.jsonl",
    }
    seen = []

    def observe(_event, evt, session_dir):
        seen.append(evt.ctx.t.attachments)
        release_transcript(evt.ctx.transcript)
        return None

    monkeypatch.setattr("captain_hook.cli.dispatch", observe)
    monkeypatch.setattr(
        "captain_hook.cli.after_reply", lambda event, evt, raw, session_dir: observe(event, evt, session_dir)
    )
    session_dir = ensure_session(SessionId("s-both"))
    for index in range(2):
        if index:
            discovery_client.results["second"] = tmp_path / "later.jsonl"
        _, background = dispatch_event(
            tmp_path,
            Event.Stop,
            {"session_id": "s-both", "transcript_path": str(tmp_path / "main.jsonl")},
            session_dir=session_dir,
            transcript_loader=lambda _: Session(()),
        )
        background()

    initial = (tmp_path / "first.jsonl", tmp_path / "second.jsonl")
    updated = (tmp_path / "first.jsonl", tmp_path / "later.jsonl")
    assert seen == [initial, initial, updated, updated]
    assert len(discovery_client.requests) == 2
    assert discovery_client.released == ["first", "second"] * 2
