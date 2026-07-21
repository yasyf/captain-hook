from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

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

        server = build_mcp_server()  # importing succeeds only with the mcp extra installed
        asyncio.run(server.call_tool("register_transcript", {"session_id": "s-mcp", "thread_id": "thread-mcp"}))
        (entry,) = read_entries("s-mcp")
        assert entry["provider"] == "codex"
        assert entry["thread_id"] == "thread-mcp"


class TestRegisteredPaths:
    def test_resolves_thread_id_against_codex_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cc_transcript import codex

        thread_id = "019f6800-3b4c-7d5e-9f60-0000000000ab"
        rollout = write_apply_patch_rollout(
            tmp_path / "codex" / "2026" / "07" / "16" / f"rollout-2026-07-16T16-44-00-{thread_id}.jsonl", thread_id
        )
        monkeypatch.setattr(codex, "SESSIONS_ROOT", tmp_path / "codex")

        register_transcript("s-res", provider="codex", thread_id=thread_id)
        (resolved,) = registered_paths(ensure_session(SessionId("s-res")))
        assert resolved.samefile(rollout)

    def test_unresolvable_thread_id_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cc_transcript import codex

        monkeypatch.setattr(codex, "SESSIONS_ROOT", tmp_path / "empty")
        register_transcript("s-miss", provider="codex", thread_id="019f6800-0000-0000-0000-000000000099")
        assert registered_paths(ensure_session(SessionId("s-miss"))) == ()

    def test_path_entry_resolves_to_itself(self, tmp_path: Path) -> None:
        rollout = tmp_path / "ext.jsonl"
        rollout.write_text("{}\n")
        register_transcript("s-direct", provider="codex", path=str(rollout))
        assert registered_paths(ensure_session(SessionId("s-direct"))) == (rollout,)

    def test_relative_path_is_anchored_to_registration_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lane = tmp_path / "lane"
        lane.mkdir()
        (lane / "rollout.jsonl").write_text("{}\n")
        monkeypatch.chdir(lane)
        register_transcript("s-rel", provider="codex", path="rollout.jsonl")

        (entry,) = read_entries("s-rel")
        stored = Path(str(entry["path"]))
        assert stored.is_absolute()
        assert stored.samefile(lane / "rollout.jsonl")

        # Dispatch runs from the project root, not the registration cwd; the absolute locator still folds in.
        monkeypatch.chdir(tmp_path)
        (resolved,) = registered_paths(ensure_session(SessionId("s-rel")))
        assert resolved.is_absolute()
        assert resolved.samefile(lane / "rollout.jsonl")


class TestLocatorValidation:
    @pytest.mark.parametrize("kwargs", [{"path": ""}, {"thread_id": ""}, {"path": "", "thread_id": ""}])
    def test_empty_locator_is_rejected_without_writing_slot(self, kwargs: dict[str, str]) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            register_transcript("s-empty-direct", provider="codex", **kwargs)
        assert not slot_path("s-empty-direct").exists()


class TestUnsafePathsSkipped:
    def test_fifo_path_is_skipped_without_hanging(self, tmp_path: Path) -> None:
        fifo = tmp_path / "rollout.fifo"
        os.mkfifo(fifo)
        register_transcript("s-fifo", provider="codex", path=str(fifo))
        assert registered_paths(ensure_session(SessionId("s-fifo"))) == ()

    def test_directory_path_is_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "adir"
        target.mkdir()
        register_transcript("s-dir", provider="codex", path=str(target))
        assert registered_paths(ensure_session(SessionId("s-dir"))) == ()

    def test_oversized_file_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        big = tmp_path / "big.jsonl"
        big.write_text("{}\n" * 16)
        monkeypatch.setattr("captain_hook.transcripts.MAX_TRANSCRIPT_BYTES", 4)
        register_transcript("s-big", provider="codex", path=str(big))
        assert registered_paths(ensure_session(SessionId("s-big"))) == ()

    def test_regular_file_within_bound_is_kept(self, tmp_path: Path) -> None:
        rollout = tmp_path / "ok.jsonl"
        rollout.write_text("{}\n")
        register_transcript("s-ok", provider="codex", path=str(rollout))
        (resolved,) = registered_paths(ensure_session(SessionId("s-ok")))
        assert resolved.samefile(rollout)


def test_dispatch_folds_registered_rollout_into_deep_gate(tmp_path: Path) -> None:
    # A codex rollout registered against the session is folded into the deep view at dispatch, so a
    # deep-predicated gate sees its apply_patch edits while the bare (main-window) gate does not.
    rollout = write_apply_patch_rollout(tmp_path / "rollout.jsonl", "thread-e2e")
    register_transcript("s-e2e", provider="codex", path=str(rollout))

    main = tmp_path / "main.jsonl"
    main.write_text(
        "\n".join(
            json.dumps(m)
            for m in (
                raw_text("user", "look at the code"),
                raw_assistant(raw_text_block("reading"), raw_tool_use("Read", {"file_path": "src/main.py"}, "tu1")),
            )
        )
        + "\n"
    )

    fired: list[str] = []

    @on(Event.Stop)
    def deep_gate(evt: object) -> None:
        if evt.ctx.t.deep.tool_calls.named("Edit|Write").files():  # type: ignore[attr-defined]
            fired.append("deep")

    @on(Event.Stop)
    def bare_gate(evt: object) -> None:
        if evt.ctx.t.tool_calls.named("Edit|Write").files():  # type: ignore[attr-defined]
            fired.append("bare")

    dispatch_event(
        tmp_path,
        Event.Stop,
        {"session_id": "s-e2e", "transcript_path": str(main)},
        session_dir=ensure_session(SessionId("s-e2e")),
        async_=False,
    )
    assert fired == ["deep"]
