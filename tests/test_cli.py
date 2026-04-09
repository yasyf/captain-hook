from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from captain_hook.app import (
    _state,
    hook as register_hook,
    on,
    register,
)
from captain_hook.types import Event

PKG_DIR = Path(__file__).resolve().parent.parent


def run_cli(
    *args: str,
    stdin_data: str = "",
    hooks_dir: str | None = None,
    root_dir: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "captain_hook"]
    if hooks_dir:
        cmd.extend(["--hooks", hooks_dir])
    if root_dir:
        cmd.extend(["--root", root_dir])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=str(PKG_DIR),
    )


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


class TestGenerateSettings:
    def test_cli_003_generate_settings(self, tmp_path: Path) -> None:
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

        result = run_cli("generate-settings", hooks_dir=str(hooks_dir))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "hooks" in data

    def test_cli_004_generate_settings_run_command(self, tmp_path: Path) -> None:
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

        result = run_cli("generate-settings", "--run-command", "path/to/hooks", hooks_dir=str(hooks_dir))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        raw = json.dumps(data)
        assert "path/to/hooks run PreToolUse" in raw

    def test_cli_008_settings_reflects_registered_events(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="pre tool")
        register_hook(Event.Stop, message="stop check")
        settings = generate_settings("bin/hooks")
        assert "PreToolUse" in settings["hooks"]
        assert "Stop" in settings["hooks"]
        assert len(settings["hooks"]) == 2

    def test_cli_009_async_hooks_produce_async_entries(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="async tool", async_=True)
        settings = generate_settings("bin/hooks")
        commands = settings["hooks"]["PreToolUse"][0]["hooks"]
        assert any(cmd.get("async") is True for cmd in commands)

    def test_cli_010_same_event_sync_async_two_commands(self) -> None:
        from captain_hook.cli import generate_settings

        register_hook(Event.PreToolUse, message="sync tool", async_=False)
        register_hook(Event.PreToolUse, message="async tool", async_=True)
        settings = generate_settings("bin/hooks")
        commands = settings["hooks"]["PreToolUse"][0]["hooks"]
        assert len(commands) == 2
        has_sync = any("async" not in cmd for cmd in commands)
        has_async = any(cmd.get("async") is True for cmd in commands)
        assert has_sync
        assert has_async


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

        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
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
            {"type": "user", "message": {"content": [{"type": "text", "text": "fix the bug"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I'll look at the code."},
                        {"type": "tool_use", "name": "Read", "id": "tu1", "input": {"file_path": "src/main.py"}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "tu2", "input": {"command": "uv run pytest"}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "id": "tu3",
                            "input": {
                                "file_path": "src/main.py",
                                "old_string": "old",
                                "new_string": "new",
                            },
                        },
                    ]
                },
            },
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
                        message=f"turn start_idx={turn.start_idx}",
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
        assert "turn start_idx=" in raw, f"unexpected output: {raw}"
        assert "turn error" not in raw, f"turn access failed: {raw}"

    def test_cli_agent_transcript_path_preferred(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")

        parent_transcript = tmp_path / "parent.jsonl"
        parent_transcript.write_text(
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "parent"}]}}) + "\n"
        )

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

    def test_cli_session_scoped_to_transcript(self, tmp_path: Path) -> None:
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

        other_transcript = tmp_path / "other_transcript.jsonl"
        self._make_transcript_jsonl(other_transcript)
        stdin_other = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "transcript_path": str(other_transcript),
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
        assert "once only" in result3.stdout, "different transcript should have fresh session"

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
