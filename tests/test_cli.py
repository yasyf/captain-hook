from __future__ import annotations

import json
import os
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import wn

from captain_hook.util import http
from tests.helpers import (
    raw_assistant,
    raw_text,
    raw_text_block,
    raw_tool_msg,
    raw_tool_use,
    run_cli,
)

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

    def test_cli_handler_transcript_load_failure_is_loud(self, tmp_path: Path, hooks_dir: Path) -> None:
        # An unreadable transcript first touched inside a handler must exit nonzero, not be
        # swallowed by dispatch's handler-error boundary into a silent empty stdout + exit 0.
        transcript = tmp_path / "transcript.jsonl"
        self._make_transcript_jsonl(transcript)
        transcript.chmod(0)

        write_hook(
            hooks_dir,
            """\
            from captain_hook.app import on
            from captain_hook.types import Event, Action, HookResult


            @on(Event.PreToolUse)
            def read_transcript(evt):
                return HookResult(action=Action.warn, message=f"{len(evt.ctx.t)} messages")
        """,
        )

        stdin = stdin_json(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            transcript_path=str(transcript),
        )
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), stdin_data=stdin)
        assert result.returncode != 0, f"expected a loud failure; got stdout={result.stdout!r}"

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


class TestNlpProvisioning:
    @pytest.fixture(autouse=True)
    def _purge_hooks_modules(self) -> Iterator[None]:
        # init_project discovers a real .claude/hooks package in-process; purge it so a stale
        # sys.modules["hooks"] does not shadow the hooks package of a later test.
        import sys

        def purge() -> None:
            for key in [k for k in sys.modules if k == "hooks" or k.startswith("hooks.")]:
                del sys.modules[key]
            sys.path[:] = [p for p in sys.path if not (Path(p) / "hooks").is_dir()]

        purge()
        yield
        purge()

    @staticmethod
    def pin_resolved_packs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, nlp: bool) -> list[Any]:
        from captain_hook.packs import manager

        pack_dir = tmp_path / "fake-pack"
        pack_dir.mkdir(exist_ok=True)
        resolved = [
            manager.ResolvedPack(
                entry=manager.BuiltinPack(name="fake"),
                path=pack_dir,
                manifest=manager.PackManifest(name="fake", version="0.1.0", description="d", hooks=".", nlp=nlp),
            )
        ]
        monkeypatch.setattr(manager, "resolve_enabled_packs", lambda _root: (resolved, []))
        return resolved

    @pytest.fixture
    def provision_mock(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from unittest.mock import MagicMock

        mock = MagicMock()
        monkeypatch.setattr("captain_hook.util.model_cache.ensure_nlp_resources", mock)
        return mock

    def test_init_provisions_nlp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provision_mock: Any) -> None:
        from captain_hook.cli import init_project

        self.pin_resolved_packs(tmp_path, monkeypatch, nlp=True)
        init_project(tmp_path, review=False)
        provision_mock.assert_called_once_with()

    def test_provision_nlp_skips_without_nlp_packs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provision_mock: Any
    ) -> None:
        from captain_hook.cli import provision_nlp

        provision_nlp(self.pin_resolved_packs(tmp_path, monkeypatch, nlp=False))
        provision_mock.assert_not_called()

    @pytest.mark.parametrize(
        "make_exc",
        [
            pytest.param(lambda: http.GitHubFetchError("offline"), id="github_fetch_error"),
            pytest.param(lambda: wn.Error("corrupt lexicon"), id="wn_error"),
            pytest.param(
                lambda: httpx.HTTPStatusError(
                    "500", request=httpx.Request("GET", "https://x"), response=httpx.Response(500)
                ),
                id="httpx_status_error",
            ),
        ],
    )
    def test_provision_nlp_defers_on_provisioning_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provision_mock: Any,
        capsys: pytest.CaptureFixture[str],
        make_exc: Any,
    ) -> None:
        from captain_hook.cli import provision_nlp

        provision_mock.side_effect = make_exc()
        provision_nlp(self.pin_resolved_packs(tmp_path, monkeypatch, nlp=True))
        assert "deferred" in capsys.readouterr().out
