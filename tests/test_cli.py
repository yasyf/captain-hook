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

BLOCK_STDIN = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hooks"
    d.mkdir()
    (d / "__init__.py").write_text("")
    return d


def write_hook(hooks_dir: Path, src: str, name: str = "my_hook.py") -> Path:
    path = hooks_dir / name
    path.write_text(textwrap.dedent(src))
    return path


def stdin_json(**raw: Any) -> str:
    return json.dumps(raw)


class TestRunSubcommand:
    def test_cli_001_run_entry_point(self, hooks_dir: Path) -> None:
        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0

    def test_cli_002_run_with_async_flag(self, hooks_dir: Path) -> None:
        (hooks_dir / "conf.py").write_text("")
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="sync hook", async_=False)
            hook(Event.PreToolUse, message="async hook", async_=True)
        """,
        )

        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})

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

    def test_cli_011_invalid_event_type(self, hooks_dir: Path) -> None:
        result = run_cli("run", "InvalidEvent", hooks_dir=str(hooks_dir), stdin_data="{}")
        assert result.returncode != 0


class TestRegisterHooks:
    def test_cli_003_register_hooks_dry_run(self, tmp_path: Path, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check tool use")
        """,
        )

        result = run_cli("register-hooks", "--dry-run", hooks_dir=str(hooks_dir), root_dir=str(tmp_path))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "hooks" in data
        raw = json.dumps(data)
        assert "uvx capt-hook run PreToolUse" in raw
        assert "--hooks" not in raw
        assert "--root" not in raw

    def test_cli_004_register_hooks_hooks_dir(self, tmp_path: Path, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check tool use")
        """,
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

    @pytest.mark.parametrize(
        ("hooks_dir_arg", "expected_command"),
        [
            pytest.param(None, "uvx capt-hook run PreToolUse", id="default_command_omits_path_flags"),
            pytest.param(
                "custom/hooks",
                "uvx capt-hook --hooks $CLAUDE_PROJECT_DIR/custom/hooks run PreToolUse",
                id="custom_hooks_dir_keeps_hooks_flag",
            ),
        ],
    )
    def test_cli_command_for_hooks_dir(self, hooks_dir_arg: str | None, expected_command: str) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="pre tool")
        settings = generate_settings(hooks_dir=hooks_dir_arg) if hooks_dir_arg else generate_settings()
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert command == expected_command

    def test_cli_027_session_end_hook_wired(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.SessionEnd, message="session over")
        entries = generate_settings()["hooks"]["SessionEnd"][0]["hooks"]
        assert entries[0]["command"] == "uvx capt-hook run SessionEnd"
        assert entries[1] == {"type": "command", "command": "uvx capt-hook review run", "async": True}

    def test_cli_021_dry_run_writes_nothing(self, tmp_path: Path, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check")
        """,
        )
        settings_path = tmp_path / ".claude" / "settings.json"
        result = run_cli("register-hooks", "--dry-run", hooks_dir=str(hooks_dir), root_dir=str(tmp_path))
        assert result.returncode == 0
        assert not settings_path.exists()
        assert "PreToolUse" in json.loads(result.stdout)["hooks"]

    def test_cli_022_writes_settings_by_default(self, tmp_path: Path, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="check")
        """,
        )
        settings_path = tmp_path / ".claude" / "settings.json"
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

    def test_cli_029_defers_events_wired_in_committed_settings(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre")
        register_hook(Event.Stop, message="stop")
        committed = 'env -u UV_EXCLUDE_NEWER uv run --project "$CLAUDE_PROJECT_DIR" capt-hook run {}'
        self.seed(
            tmp_path / "settings.json",
            {
                event: [{"hooks": [{"type": "command", "command": committed.format(event)}]}]
                for event in ("PreToolUse", "Stop")
            },
        )

        merged, summary = merge_settings(".claude/hooks", tmp_path / "settings.local.json")
        assert "PreToolUse" not in merged["hooks"]
        assert "Stop" not in merged["hooks"]
        assert "SessionEnd" in merged["hooks"]
        assert summary["PreToolUse"] == "deferred"
        assert summary["Stop"] == "deferred"
        assert summary["SessionEnd"] == "added"

    def test_cli_030_strips_local_duplicate_of_committed(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre")
        self.seed(
            tmp_path / "settings.json",
            {"PreToolUse": [{"hooks": [{"type": "command", "command": "uvx capt-hook run PreToolUse"}]}]},
        )
        sp = tmp_path / "settings.local.json"
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-tool"}]}
        dup = {"hooks": [{"type": "command", "command": "uvx capt-hook run PreToolUse"}]}
        self.seed(sp, {"PreToolUse": [foreign, dup]})

        merged, summary = merge_settings(".claude/hooks", sp)
        assert merged["hooks"]["PreToolUse"] == [foreign]
        assert summary["PreToolUse"] == "deferred"

    def test_cli_031_defers_events_wired_in_local_settings(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre")
        register_hook(Event.Stop, message="stop")
        self.seed(
            tmp_path / "settings.local.json",
            {
                event: [{"hooks": [{"type": "command", "command": f"uvx capt-hook run {event}"}]}]
                for event in ("PreToolUse", "Stop")
            },
        )

        merged, summary = merge_settings(".claude/hooks", tmp_path / "settings.json")
        assert "PreToolUse" not in merged["hooks"]
        assert "Stop" not in merged["hooks"]
        assert "SessionEnd" in merged["hooks"]
        assert summary["PreToolUse"] == "deferred"
        assert summary["Stop"] == "deferred"
        assert summary["SessionEnd"] == "added"

    def test_cli_032_strips_committed_duplicate_of_local(self, tmp_path: Path) -> None:
        from captain_hook.cli import merge_settings

        register_hook(Event.PreToolUse, message="pre")
        self.seed(
            tmp_path / "settings.local.json",
            {"PreToolUse": [{"hooks": [{"type": "command", "command": "uvx capt-hook run PreToolUse"}]}]},
        )
        sp = tmp_path / "settings.json"
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-tool"}]}
        dup = {"hooks": [{"type": "command", "command": "uvx capt-hook run PreToolUse"}]}
        self.seed(sp, {"PreToolUse": [foreign, dup]})

        merged, summary = merge_settings(".claude/hooks", sp)
        assert merged["hooks"]["PreToolUse"] == [foreign]
        assert summary["PreToolUse"] == "deferred"


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

    @pytest.mark.parametrize(
        ("registered", "wired", "expected"),
        [
            pytest.param(
                (Event.PreToolUse, Event.UserPromptSubmit),
                ("PreToolUse",),
                {"UserPromptSubmit"},
                id="flags_unwired_event",
            ),
            pytest.param(
                (Event.SessionEnd,),
                ("PreToolUse",),
                {"SessionEnd"},
                id="flags_unwired_session_end",
            ),
            pytest.param(
                (Event.PreToolUse,),
                ("PreToolUse",),
                set(),
                id="no_drift_when_all_wired",
            ),
        ],
    )
    def test_cli_drift(
        self, tmp_path: Path, registered: tuple[Event, ...], wired: tuple[str, ...], expected: set[str]
    ) -> None:
        from captain_hook.cli import settings_drift

        for event in registered:
            register_hook(event, message="m")
        self.write_settings(tmp_path, *wired)
        assert settings_drift(tmp_path) == expected

    def test_cli_019_no_drift_without_settings_file(self, tmp_path: Path) -> None:
        from captain_hook.cli import settings_drift

        register_hook(Event.UserPromptSubmit, message="ups")
        assert settings_drift(tmp_path) == set()

    def test_cli_031_no_drift_when_committed_covers_omitted_events(self, tmp_path: Path) -> None:
        from captain_hook.cli import settings_drift

        register_hook(Event.PreToolUse, message="pre")
        register_hook(Event.Stop, message="stop")
        register_hook(Event.PostToolUse, message="post")
        self.write_settings(tmp_path, "PreToolUse", "Stop")
        (tmp_path / ".claude" / "settings.local.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PostToolUse": [{"hooks": [{"type": "command", "command": "uvx capt-hook run PostToolUse"}]}]
                    }
                }
            )
        )
        assert settings_drift(tmp_path) == set()

    def test_cli_020_run_surfaces_drift_to_agent_once(self, tmp_path: Path, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="pre tool fired")
            hook(Event.UserPromptSubmit, message="prompt hook")
        """,
        )
        root_dir = tmp_path / "project"
        self.write_settings(root_dir, "PreToolUse")

        stdin = stdin_json(session_id="drift-sess", tool_name="Bash", tool_input={"command": "echo hi"})
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

    def test_empty_stdin_no_output(self, hooks_dir: Path) -> None:
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data="")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_malformed_json_no_crash(self, hooks_dir: Path) -> None:
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data="not json")
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "Malformed" in result.stderr or "malformed" in result.stderr.lower() or "error" in result.stderr.lower()


class TestCLIWithContext:
    def test_cli_run_creates_hook_context(self, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_ctx(evt):
                if evt.ctx is None:
                    return HookResult(action=Action.block, message="ctx is None!")
                return HookResult(action=Action.warn, message="ctx exists")
        """,
        )

        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            output = json.loads(result.stdout)
            raw = json.dumps(output)
            assert "ctx exists" in raw
            assert "ctx is None" not in raw

    @pytest.mark.parametrize(
        ("accessor", "ok_message"),
        [
            pytest.param("evt.ctx.s", "session store ok", id="hook_accesses_session_store"),
            pytest.param("evt.ctx.state", "state accessor ok", id="hook_accesses_state"),
        ],
    )
    def test_cli_hook_accesses_session_store_type(self, hooks_dir: Path, accessor: str, ok_message: str) -> None:
        write_hook(
            hooks_dir,
            f"""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_store(evt):
                try:
                    store = {accessor}
                    from captain_hook.session import SessionStore
                    if not isinstance(store, SessionStore):
                        return HookResult(action=Action.block, message="not a SessionStore")
                    return HookResult(action=Action.warn, message="{ok_message}")
                except Exception as e:
                    return HookResult(action=Action.block, message=f"error: {{e}}")
        """,
        )

        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            output = json.loads(result.stdout)
            raw = json.dumps(output)
            assert ok_message in raw


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

    def test_cli_extracts_transcript_path_from_stdin(self, tmp_path: Path, hooks_dir: Path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)

        write_hook(
            hooks_dir,
            """\
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
        """,
        )

        stdin = stdin_json(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            transcript_path=str(transcript),
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip()
        output = json.loads(result.stdout)
        raw = json.dumps(output)
        assert "transcript has 4 messages" in raw, f"unexpected output: {raw}"

    def test_cli_ctx_turn_accessible(self, tmp_path: Path, hooks_dir: Path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)

        write_hook(
            hooks_dir,
            """\
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
        """,
        )

        stdin = stdin_json(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            transcript_path=str(transcript),
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip()
        output = json.loads(result.stdout)
        raw = json.dumps(output)
        assert "turn events=4" in raw, f"unexpected output: {raw}"
        assert "prompt=fix the bug" in raw, f"unexpected output: {raw}"
        assert "turn error" not in raw, f"turn access failed: {raw}"

    def test_cli_agent_transcript_path_preferred(self, tmp_path: Path, hooks_dir: Path) -> None:
        parent_transcript = tmp_path / "parent.jsonl"
        parent_transcript.write_text(json.dumps(raw_text("user", "parent")) + "\n")

        agent_transcript = tmp_path / "agent.jsonl"
        self._make_transcript_jsonl(agent_transcript)

        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_agent_transcript(evt):
                t = evt.ctx.t
                return HookResult(
                    action=Action.warn,
                    message=f"transcript has {len(t)} messages",
                )
        """,
        )

        stdin = stdin_json(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            transcript_path=str(parent_transcript),
            agent_transcript_path=str(agent_transcript),
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        assert result.stdout.strip()
        output = json.loads(result.stdout)
        raw = json.dumps(output)
        assert "transcript has 4 messages" in raw, f"expected agent transcript (4 msgs), got: {raw}"

    def test_cli_session_scoped_to_session_id(self, tmp_path: Path, hooks_dir: Path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)

        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="once only", max_fires=1)
        """,
        )

        stdin = stdin_json(
            session_id="scoped-sess-a",
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            transcript_path=str(transcript),
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

        stdin_other = stdin_json(
            session_id="scoped-sess-b",
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            transcript_path=str(transcript),
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

    def test_cli_no_transcript_path_still_works(self, hooks_dir: Path) -> None:
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def check_ctx(evt):
                if evt.ctx is None:
                    return HookResult(action=Action.block, message="ctx is None")
                if evt.ctx.t is None:
                    return HookResult(action=Action.warn, message="transcript is None ok")
                return HookResult(action=Action.warn, message="transcript exists")
        """,
        )

        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
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
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="custom hook activated", block=True)
        """,
        )

        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode == 0
        if result.stdout.strip():
            data = json.loads(result.stdout)
            assert "custom hook activated" in json.dumps(data)

    def test_cli_007_root_flag(self, tmp_path: Path, hooks_dir: Path) -> None:
        root_dir = tmp_path / "project_root"
        root_dir.mkdir()
        gitignore = root_dir / ".gitignore"
        gitignore.write_text("node_modules\n")

        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook, on
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="should be gitignored", block=True)
        """,
        )

        stdin = stdin_json(
            tool_name="Edit",
            tool_input={
                "file_path": "node_modules/pkg/index.js",
                "old_string": "a",
                "new_string": "b",
            },
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

    @pytest.mark.parametrize(
        ("kwargs", "expected_out"),
        [
            pytest.param({}, "NEW-1\nNEW-2\nNEW-3\n", id="no_arg_prints_newest"),
            pytest.param({"tail": 2}, "NEW-2\nNEW-3\n", id="tail_limits_to_last_n_lines"),
            pytest.param({"session": "old"}, "OLD-A\nOLD-B\n", id="session_id_selects_that_file"),
        ],
    )
    def test_seeded_logs_print(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        kwargs: dict[str, Any],
        expected_out: str,
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        self.seed_logs(tmp_path)
        show_logs(**kwargs)
        assert capsys.readouterr().out == expected_out

    def test_session_transcript_path_resolves_via_stem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from captain_hook.cli import show_logs

        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path))
        (tmp_path / "t.log").write_text("BY-STEM")
        show_logs(session="/tmp/t.jsonl")
        assert capsys.readouterr().out == "BY-STEM\n"

    @pytest.mark.parametrize(
        ("log_dir_name", "kwargs", "needle_is_dir"),
        [
            pytest.param("nope", {}, True, id="missing_dir_prints_to_stderr"),
            pytest.param(None, {}, True, id="empty_dir_prints_to_stderr"),
            pytest.param(None, {"session": "absent"}, False, id="missing_file_prints_to_stderr"),
        ],
    )
    def test_logs_print_to_stderr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        log_dir_name: str | None,
        kwargs: dict[str, Any],
        needle_is_dir: bool,
    ) -> None:
        from captain_hook.cli import show_logs

        log_dir = tmp_path / log_dir_name if log_dir_name else tmp_path
        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(log_dir))
        show_logs(**kwargs)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert (str(log_dir) if needle_is_dir else kwargs["session"]) in captured.err


class TestDefaultResolution:
    @staticmethod
    def scaffold_project(root: Path) -> None:
        hooks_dir = root / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "__init__.py").write_text("")
        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import hook
            from captain_hook.types import Event

            hook(Event.PreToolUse, message="default hooks dir resolved", block=True)
        """,
        )

    def test_cli_014_default_hooks_dir_from_claude_project_dir(self, tmp_path: Path) -> None:
        self.scaffold_project(tmp_path)
        result = run_cli(
            "run",
            "PreToolUse",
            stdin_data=BLOCK_STDIN,
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
            stdin_data=BLOCK_STDIN,
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
            stdin_data=BLOCK_STDIN,
            env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "default hooks dir resolved" not in result.stdout
