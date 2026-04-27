from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from captain_hook.app import _state, get_matching_hooks
from captain_hook.dispatch import execute_hook
from captain_hook.primitives.audit import audit, session_id_for
from captain_hook.tests.helpers import make_ctx, make_post_tool_event, make_pre_tool_event, make_stop_event
from captain_hook.types import Event, FilePath


class TestRegistration:
    def test_registers_one_hook_for_combined_events(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse | Event.PostToolUse | Event.Stop, log_dir=tmp_path)
        assert len(_state.hooks) == 1
        assert _state.hooks[-1].spec.events == Event.PreToolUse | Event.PostToolUse | Event.Stop

    def test_default_event_mask(self, tmp_path: Path) -> None:
        audit(log_dir=tmp_path)
        assert _state.hooks[-1].spec.events == Event.PreToolUse | Event.PostToolUse | Event.Stop


class TestDefaultFields:
    def test_writes_default_fields_to_jsonl(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse, log_dir=tmp_path)
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "ls"}, ctx)
        for entry in get_matching_hooks(evt):
            execute_hook(entry, evt, tmp_path)
        log_file = tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
        assert log_file.exists()
        record = json.loads(log_file.read_text().strip())
        assert record.keys() == {"ts", "event", "tool", "file", "session_id"}
        assert record["event"] == "PreToolUse"
        assert record["tool"] == "Bash"
        assert record["file"] is None

    def test_records_file_path_for_edit_events(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse, log_dir=tmp_path)
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Edit", {"file_path": "/x/y.py", "old_string": "a", "new_string": "b"}, ctx)
        for entry in get_matching_hooks(evt):
            execute_hook(entry, evt, tmp_path)
        record = json.loads((tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").read_text().strip())
        assert record["file"] == "/x/y.py"

    def test_appends_one_line_per_event(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse | Event.PostToolUse, log_dir=tmp_path)
        ctx = make_ctx(tmp_path)
        for evt in (
            make_pre_tool_event("Bash", {"command": "ls"}, ctx),
            make_post_tool_event("Bash", {"command": "ls"}, ctx),
        ):
            for entry in get_matching_hooks(evt):
                execute_hook(entry, evt, tmp_path)
        lines = (tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").read_text().splitlines()
        assert [json.loads(line)["event"] for line in lines] == ["PreToolUse", "PostToolUse"]


class TestCustomFields:
    def test_custom_fields_callable_replaces_payload(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse, log_dir=tmp_path, fields=lambda evt: {"only": evt.tool_name})
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "ls"}, ctx)
        for entry in get_matching_hooks(evt):
            execute_hook(entry, evt, tmp_path)
        record = json.loads((tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").read_text().strip())
        assert record == {"only": "Bash"}


class TestCustomFilename:
    def test_custom_filename_callable(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse, log_dir=tmp_path, filename=lambda _: "fixed.jsonl")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "ls"}, ctx)
        for entry in get_matching_hooks(evt):
            execute_hook(entry, evt, tmp_path)
        assert (tmp_path / "fixed.jsonl").exists()


class TestDefaultLogDir:
    def test_default_uses_claude_project_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        audit(Event.PreToolUse)
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "ls"}, ctx)
        for entry in get_matching_hooks(evt):
            execute_hook(entry, evt, tmp_path)
        log_file = tmp_path / ".context" / "hook-logs" / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
        assert log_file.exists()


class TestConditionFiltering:
    def test_only_if_filters_events(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse, log_dir=tmp_path, only_if=[FilePath("*.py")])
        ctx = make_ctx(tmp_path)
        bash = make_pre_tool_event("Bash", {"command": "ls"}, ctx)
        edit = make_pre_tool_event("Edit", {"file_path": "/x/y.py", "old_string": "a", "new_string": "b"}, ctx)
        for evt in (bash, edit):
            for entry in get_matching_hooks(evt):
                execute_hook(entry, evt, tmp_path)
        records = [json.loads(l) for l in (tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").read_text().splitlines()]
        assert [r["tool"] for r in records] == ["Edit"]

    def test_skip_if_excludes_events(self, tmp_path: Path) -> None:
        audit(Event.PreToolUse, log_dir=tmp_path, skip_if=[FilePath("*.py")])
        ctx = make_ctx(tmp_path)
        bash = make_pre_tool_event("Bash", {"command": "ls"}, ctx)
        edit = make_pre_tool_event("Edit", {"file_path": "/x/y.py", "old_string": "a", "new_string": "b"}, ctx)
        for evt in (bash, edit):
            for entry in get_matching_hooks(evt):
                execute_hook(entry, evt, tmp_path)
        records = [json.loads(l) for l in (tmp_path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").read_text().splitlines()]
        assert [r["tool"] for r in records] == ["Bash"]


class TestSessionId:
    def test_session_id_for_returns_12_hex_chars(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.t.path = Path("/tmp/some/session.jsonl")
        evt = make_stop_event(ctx)
        sid = session_id_for(evt)
        assert sid is not None
        assert len(sid) == 12
        assert all(c in "0123456789abcdef" for c in sid)

    def test_session_id_for_returns_none_without_path(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        evt = make_stop_event(ctx)
        assert session_id_for(evt) is None

    def test_session_id_stable_across_calls(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.t.path = Path("/tmp/x.jsonl")
        evt = make_stop_event(ctx)
        assert session_id_for(evt) == session_id_for(evt)
