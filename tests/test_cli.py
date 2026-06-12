from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import (
    hook as register_hook,
)
from captain_hook.tests.helpers import (
    raw_assistant,
    raw_text,
    raw_text_block,
    raw_tool_msg,
    raw_tool_use,
    run_cli,
)
from captain_hook.types import Event


class TestRunSubcommand:
    def test_cli_001_run_entry_point(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0

    def test_cli_002_run_with_async_flag(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        conf_py = hooks_dir / "conf.py"
        conf_py.write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="sync hook", async_=False)
            hook(Event.PreToolUse, message="async hook", async_=True)
        """)
        )

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})

        result_sync = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result_sync.returncode == 0
        if result_sync.stdout.strip():
            output_sync = json.loads(result_sync.stdout)
            assert "sync hook" in json.dumps(output_sync)

        result_async = run_cli("run", "PreToolUse", "--async", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result_async.returncode == 0
        if result_async.stdout.strip():
            output_async = json.loads(result_async.stdout)
            assert "async hook" in json.dumps(output_async)

    def test_cli_011_invalid_event_type(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        result = run_cli("run", "InvalidEvent", hooks_dir=str(hooks_dir), stdin_data="{}")
        assert result.returncode != 0


class TestRegisterHooks:
    def test_cli_003_register_hooks_dry_run(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check tool use")
        """)
        )

        result = run_cli("register-hooks", "--dry-run", hooks_dir=str(hooks_dir), root_dir=str(tmp_path))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "hooks" in data
        raw = json.dumps(data)
        assert "uvx capt-hook run PreToolUse" in raw
        assert "--hooks" not in raw
        assert "--root" not in raw

    def test_cli_004_register_hooks_hooks_dir(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check tool use")
        """)
        )

        result = run_cli(
            "register-hooks",
            "--hooks-dir",
            "custom/hooks",
            "--dry-run",
            hooks_dir=str(hooks_dir),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        raw = json.dumps(data)
        assert "$CLAUDE_PROJECT_DIR/custom/hooks" in raw

    def test_cli_008_settings_reflects_registered_events(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="pre tool")
        register_hook(Event.Stop, message="stop check")
        settings = generate_settings()
        assert "PreToolUse" in settings["hooks"]
        assert "Stop" in settings["hooks"]
        assert "SessionEnd" in settings["hooks"]
        assert len(settings["hooks"]) == 3

    def test_cli_009_async_hooks_produce_async_entries(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="async tool", async_=True)
        settings = generate_settings()
        commands = settings["hooks"]["PreToolUse"][0]["hooks"]
        assert any(cmd.get("async") is True for cmd in commands)

    def test_cli_010_same_event_sync_async_two_commands(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="sync tool", async_=False)
        register_hook(Event.PreToolUse, message="async tool", async_=True)
        settings = generate_settings()
        commands = settings["hooks"]["PreToolUse"][0]["hooks"]
        assert len(commands) == 2
        has_sync = any("async" not in cmd for cmd in commands)
        has_async = any(cmd.get("async") is True for cmd in commands)
        assert has_sync
        assert has_async

    def test_cli_012_default_command_omits_path_flags(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="pre tool")
        command = generate_settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert command == "uvx capt-hook run PreToolUse"

    def test_cli_013_custom_hooks_dir_keeps_hooks_flag(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="pre tool")
        command = generate_settings(hooks_dir="custom/hooks")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert command == "uvx capt-hook --hooks $CLAUDE_PROJECT_DIR/custom/hooks run PreToolUse"

    def test_cli_027_session_end_hook_wired(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.SessionEnd, message="session over")
        command = generate_settings()["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        assert command == "uvx capt-hook run SessionEnd"

    def test_cli_021_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check")
        """)
        )
        settings_path = tmp_path / ".claude" / "settings.local.json"
        result = run_cli("register-hooks", "--dry-run", hooks_dir=str(hooks_dir), root_dir=str(tmp_path))
        assert result.returncode == 0
        assert not settings_path.exists()
        assert "PreToolUse" in json.loads(result.stdout)["hooks"]

    def test_cli_022_writes_settings_by_default(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check")
        """)
        )
        settings_path = tmp_path / ".claude" / "settings.local.json"
        result = run_cli("register-hooks", hooks_dir=str(hooks_dir), root_dir=str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(settings_path.read_text())
        assert "PreToolUse" in data["hooks"]
        assert "+ added PreToolUse" in result.stdout


class TestMergeSettings:
    @staticmethod
    def seed(path: Path, hooks: dict[str, Any], **extra: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": hooks} | extra))

    def test_cli_023_preserves_foreign_and_adds_own(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre")
        sp = tmp_path / "settings.local.json"
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-tool"}]}
        self.seed(sp, {"PreToolUse": [foreign]}, customKey="keep-me")

        merged, summary = merge_settings(".claude/hooks", sp)
        groups = merged["hooks"]["PreToolUse"]
        assert foreign in groups
        assert any(h["command"] == "uvx capt-hook run PreToolUse" for g in groups for h in g["hooks"])
        assert merged["customKey"] == "keep-me"
        assert summary["PreToolUse"] == "added"

    def test_cli_024_refreshes_changed_own(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre", async_=True)
        sp = tmp_path / "settings.local.json"
        self.seed(sp, {"PreToolUse": [{"hooks": [{"type": "command", "command": "uvx capt-hook run PreToolUse"}]}]})

        merged, summary = merge_settings(".claude/hooks", sp)
        commands = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert commands == ["uvx capt-hook run PreToolUse --async"]
        assert summary["PreToolUse"] == "updated"

    def test_cli_025_removes_stale_own_keeps_foreign(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre")
        sp = tmp_path / "settings.local.json"
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "keep"}]}
        stale = {"hooks": [{"type": "command", "command": "uvx capt-hook run PostToolUse"}]}
        self.seed(sp, {"PostToolUse": [foreign, stale]})

        merged, summary = merge_settings(".claude/hooks", sp)
        assert summary["PostToolUse"] == "removed"
        assert merged["hooks"]["PostToolUse"] == [foreign]

    def test_cli_026_idempotent(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings, write_settings

        register_hook(Event.PreToolUse, message="pre")
        register_hook(Event.Stop, message="stop")
        sp = tmp_path / "settings.local.json"

        write_settings(sp, merge_settings(".claude/hooks", sp)[0])
        first = sp.read_text()
        merged2, summary2 = merge_settings(".claude/hooks", sp)
        write_settings(sp, merged2)
        assert sp.read_text() == first
        assert set(summary2.values()) == {"unchanged"}


class TestSettingsDrift:
    @staticmethod
    def write_settings(root: Path, *wired_events: str) -> None:
        claude = root / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [{"hooks": [{"type": "command", "command": f"uvx capt-hook run {event}"}]}]
                        for event in wired_events
                    }
                }
            )
        )

    def test_cli_017_drift_flags_unwired_event(self, tmp_path: Path) -> None:
        from captain_hook.cli import settings_drift

        register_hook(Event.PreToolUse, message="pre")
        register_hook(Event.UserPromptSubmit, message="ups")
        self.write_settings(tmp_path, "PreToolUse")
        assert settings_drift(tmp_path) == {"UserPromptSubmit"}

    def test_cli_028_drift_flags_unwired_session_end(self, tmp_path: Path) -> None:
        from captain_hook.cli import settings_drift

        register_hook(Event.SessionEnd, message="session over")
        self.write_settings(tmp_path, "PreToolUse")
        assert settings_drift(tmp_path) == {"SessionEnd"}

    def test_cli_018_no_drift_when_all_wired(self, tmp_path: Path) -> None:
        from captain_hook.cli import settings_drift

        register_hook(Event.PreToolUse, message="pre")
        self.write_settings(tmp_path, "PreToolUse")
        assert settings_drift(tmp_path) == set()

    def test_cli_019_no_drift_without_settings_file(self, tmp_path: Path) -> None:
        from captain_hook.cli import settings_drift

        register_hook(Event.UserPromptSubmit, message="ups")
        assert settings_drift(tmp_path) == set()

    def test_cli_020_run_surfaces_drift_to_agent_once(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="pre tool fired")
            hook(Event.UserPromptSubmit, message="prompt hook")
        """)
        )
        root_dir = tmp_path / "project"
        self.write_settings(root_dir, "PreToolUse")

        stdin = json.dumps({"session_id": "drift-sess", "tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        first = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), root_dir=str(root_dir), stdin_data=stdin)
        assert first.returncode == 0
        assert "UserPromptSubmit" in first.stdout
        assert "register-hooks" in first.stdout

        second = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), root_dir=str(root_dir), stdin_data=stdin)
        assert second.returncode == 0
        assert "pre tool fired" in second.stdout
        assert "UserPromptSubmit" not in second.stdout


class TestErrorHandling:
    def test_cli_005_unknown_subcommand(self) -> None:
        result = run_cli("nonexistent")
        assert result.returncode != 0

    def test_empty_stdin_no_output(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data="")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_malformed_json_no_crash(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data="not json")
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "Malformed" in result.stderr or "malformed" in result.stderr.lower() or "error" in result.stderr.lower()


class TestCLIWithContext:
    def test_cli_run_creates_hook_context(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_ctx(evt):
                if evt.ctx is None:
                    return HookResult(action=Action.block, message="ctx is None!")
                return HookResult(action=Action.warn, message="ctx exists")
        """)
        )

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            output = json.loads(result.stdout)
            raw = json.dumps(output)
            assert "ctx exists" in raw
            assert "ctx is None" not in raw

    def test_cli_max_fires_persistent(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="once only", max_fires=1)
        """)
        )

        stdin = json.dumps(
            {"session_id": "max-fires-sess", "tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        )
        root_dir = tmp_path / "project"
        root_dir.mkdir()

        result1 = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(hooks_dir),
            root_dir=str(root_dir),
            stdin_data=stdin,
        )
        assert result1.returncode == 0
        assert "once only" in result1.stdout

        result2 = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(hooks_dir),
            root_dir=str(root_dir),
            stdin_data=stdin,
        )
        assert result2.returncode == 0
        assert result2.stdout.strip() == ""

    def test_cli_hook_accesses_session_store(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_session_store(evt):
                try:
                    store = evt.ctx.s
                    from captain_hook.session import SessionStore
                    if not isinstance(store, SessionStore):
                        return HookResult(action=Action.block, message="not a SessionStore")
                    return HookResult(action=Action.warn, message="session store ok")
                except Exception as e:
                    return HookResult(action=Action.block, message=f"error: {e}")
        """)
        )

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            output = json.loads(result.stdout)
            raw = json.dumps(output)
            assert "session store ok" in raw

    def test_cli_hook_accesses_state(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_state_accessor(evt):
                try:
                    state = evt.ctx.state
                    from captain_hook.session import SessionStore
                    if not isinstance(state, SessionStore):
                        return HookResult(action=Action.block, message="not a SessionStore")
                    return HookResult(action=Action.warn, message="state accessor ok")
                except Exception as e:
                    return HookResult(action=Action.block, message=f"error: {e}")
        """)
        )

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            output = json.loads(result.stdout)
            raw = json.dumps(output)
            assert "state accessor ok" in raw


class TestTranscriptWiring:
    @staticmethod
    def _make_transcript_jsonl(path: Path) -> None:
        messages = [
            raw_text("user", "fix the bug"),
            raw_assistant(
                raw_text_block("I'll look at the code."),
                raw_tool_use("Read", {"file_path": "src/main.py"}, "tu1"),
            ),
            raw_tool_msg("Bash", {"command": "uv run pytest"}, "tu2"),
            raw_tool_msg(
                "Edit",
                {"file_path": "src/main.py", "old_string": "old", "new_string": "new"},
                "tu3",
            ),
        ]
        path.write_text("\n".join(json.dumps(m) for m in messages) + "\n")

    def test_cli_extracts_transcript_path_from_stdin(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")

        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)

        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_transcript(evt):
                t = evt.ctx.t
                if t is None:
                    return HookResult(action=Action.block, message="transcript is None")
                if len(t) == 0:
                    return HookResult(action=Action.block, message="transcript is empty")
                return HookResult(action=Action.warn, message=f"transcript has {len(t)} messages")
        """)
        )

        stdin = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "transcript_path": str(transcript),
            }
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip()
        output = json.loads(result.stdout)
        raw = json.dumps(output)
        assert "transcript has 4 messages" in raw, f"unexpected output: {raw}"

    def test_cli_ctx_turn_accessible(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")

        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)

        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_turn(evt):
                try:
                    turn = evt.ctx.turn
                    return HookResult(
                        action=Action.warn,
                        message=f"turn events={len(turn)} prompt={turn.user_text}",
                    )
                except Exception as e:
                    return HookResult(action=Action.block, message=f"turn error: {e}")
        """)
        )

        stdin = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "transcript_path": str(transcript),
            }
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip()
        output = json.loads(result.stdout)
        raw = json.dumps(output)
        assert "turn events=4" in raw, f"unexpected output: {raw}"
        assert "prompt=fix the bug" in raw, f"unexpected output: {raw}"
        assert "turn error" not in raw, f"turn access failed: {raw}"

    def test_cli_agent_transcript_path_preferred(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")

        parent_transcript = tmp_path / "parent.jsonl"
        parent_transcript.write_text(json.dumps(raw_text("user", "parent")) + "\n")

        agent_transcript = tmp_path / "agent.jsonl"
        self._make_transcript_jsonl(agent_transcript)

        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_agent_transcript(evt):
                t = evt.ctx.t
                return HookResult(
                    action=Action.warn,
                    message=f"transcript has {len(t)} messages",
                )
        """)
        )

        stdin = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "transcript_path": str(parent_transcript),
                "agent_transcript_path": str(agent_transcript),
            }
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip()
        output = json.loads(result.stdout)
        raw = json.dumps(output)
        assert "transcript has 4 messages" in raw, f"expected agent transcript (4 msgs), got: {raw}"

    def test_cli_session_scoped_to_session_id(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")

        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)

        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="once only", max_fires=1)
        """)
        )

        stdin = json.dumps(
            {
                "session_id": "scoped-sess-a",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "transcript_path": str(transcript),
            }
        )
        root_dir = tmp_path / "project"
        root_dir.mkdir()

        result1 = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(hooks_dir),
            root_dir=str(root_dir),
            stdin_data=stdin,
        )
        assert result1.returncode == 0
        assert "once only" in result1.stdout

        result2 = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(hooks_dir),
            root_dir=str(root_dir),
            stdin_data=stdin,
        )
        assert result2.returncode == 0
        assert result2.stdout.strip() == ""

        stdin_other = json.dumps(
            {
                "session_id": "scoped-sess-b",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "transcript_path": str(transcript),
            }
        )
        result3 = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(hooks_dir),
            root_dir=str(root_dir),
            stdin_data=stdin_other,
        )
        assert result3.returncode == 0
        assert "once only" in result3.stdout, "different session id should have fresh session"

    def test_cli_no_transcript_path_still_works(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_ctx(evt):
                if evt.ctx is None:
                    return HookResult(action=Action.block, message="ctx is None")
                if evt.ctx.t is None:
                    return HookResult(action=Action.warn, message="transcript is None ok")
                return HookResult(action=Action.warn, message="transcript exists")
        """)
        )

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            raw = json.dumps(json.loads(result.stdout))
            assert "ctx is None" not in raw


class TestFlags:
    def test_cli_006_hooks_flag(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "custom_hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="custom hook activated", block=True)
        """)
        )

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            data = json.loads(result.stdout)
            assert "custom hook activated" in json.dumps(data)

    def test_cli_007_root_flag(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")

        root_dir = tmp_path / "project_root"
        root_dir.mkdir()
        gitignore = root_dir / ".gitignore"
        gitignore.write_text("node_modules\n")

        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="should be gitignored", block=True)
        """)
        )

        stdin = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "node_modules/pkg/index.js",
                    "old_string": "a",
                    "new_string": "b",
                },
            }
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), root_dir=str(root_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestLogsSubcommand:
    @staticmethod
    def seed_logs(log_dir: Path) -> None:
        (log_dir / "old.log").write_text("OLD-A\nOLD-B")
        (log_dir / "new.log").write_text("NEW-1\nNEW-2\nNEW-3")
        os.utime(log_dir / "old.log", (1000, 1000))
        os.utime(log_dir / "new.log", (2000, 2000))

    def test_no_arg_prints_newest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        self.seed_logs(tmp_path)
        show_logs()
        assert capsys.readouterr().out == "NEW-1\nNEW-2\nNEW-3\n"

    def test_tail_limits_to_last_n_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        self.seed_logs(tmp_path)
        show_logs(tail=2)
        assert capsys.readouterr().out == "NEW-2\nNEW-3\n"

    def test_session_id_selects_that_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        self.seed_logs(tmp_path)
        show_logs(session="old")
        assert capsys.readouterr().out == "OLD-A\nOLD-B\n"

    def test_session_transcript_path_resolves_via_stem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        (tmp_path / "t.log").write_text("BY-STEM")
        show_logs(session="/tmp/t.jsonl")
        assert capsys.readouterr().out == "BY-STEM\n"

    def test_missing_dir_prints_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        missing = tmp_path / "nope"
        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(missing))
        show_logs()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert str(missing) in captured.err

    def test_empty_dir_prints_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        show_logs()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert str(tmp_path) in captured.err

    def test_missing_file_prints_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        show_logs(session="absent")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "absent" in captured.err


class TestDefaultResolution:
    BLOCK_STDIN = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})

    @staticmethod
    def scaffold_project(root: Path) -> None:
        hooks_dir = root / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="default hooks dir resolved", block=True)
        """)
        )

    def test_cli_014_default_hooks_dir_from_claude_project_dir(self, tmp_path: Path) -> None:
        self.scaffold_project(tmp_path)
        result = run_cli(
            "run",
            "PreToolUse",
            stdin_data=self.BLOCK_STDIN,
            env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "default hooks dir resolved" in result.stdout

    def test_cli_015_default_hooks_dir_falls_back_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.scaffold_project(tmp_path)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        result = run_cli(
            "run",
            "PreToolUse",
            stdin_data=self.BLOCK_STDIN,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert "default hooks dir resolved" in result.stdout

    def test_cli_016_explicit_hooks_flag_overrides_default(self, tmp_path: Path) -> None:
        self.scaffold_project(tmp_path)
        empty_hooks = tmp_path / "empty"
        empty_hooks.mkdir()
        (empty_hooks / "__init__.py").write_text("")
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(empty_hooks),
            stdin_data=self.BLOCK_STDIN,
            env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "default hooks dir resolved" not in result.stdout
