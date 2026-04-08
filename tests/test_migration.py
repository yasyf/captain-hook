from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import HookApp, _current_app
from captain_hook.dispatch import execute_hook
from captain_hook.events import (
    PostToolUseEvent,
    PreToolUseEvent,
    StopEvent,
)
from captain_hook.file import File
from captain_hook.session import SessionStore
from captain_hook.transcript import Transcript
from captain_hook.types import Action


def _reload_module(name: str) -> None:
    import importlib
    import sys

    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)


def make_ctx(
    tmp_path: Path | None = None,
    transcript: Any = None,
    settings: Any = None,
    transcript_len: int = 10,
) -> MagicMock:
    ctx = MagicMock()
    if transcript is not None:
        ctx.transcript = transcript
        ctx.t = transcript
    else:
        ctx.transcript = MagicMock()
        ctx.transcript.__len__ = MagicMock(return_value=transcript_len)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_edit_to = MagicMock(return_value=False)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.count_tools = MagicMock(return_value=0)
        ctx.transcript.extract_files = MagicMock(return_value=[])
        ctx.transcript.assistant_text = MagicMock(return_value="")
        ctx.transcript.full_text = ""
        ctx.t = ctx.transcript
    store = SessionStore(tmp_path)
    ctx.session = store
    ctx.s = store
    ctx.conf = settings
    ctx.c = settings
    turn = MagicMock()
    turn.start_idx = 5
    ctx.turn = turn
    return ctx


def make_pre_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PreToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PreToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_post_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


EDIT_INPUT: dict[str, str] = {
    "file_path": "x.py",
    "old_string": "",
    "new_string": "",
}


def make_stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx or make_ctx())


@pytest.fixture
def app(tmp_path: Path) -> HookApp:
    a = HookApp()
    token = _current_app.set(a)
    yield a  # type: ignore[misc]
    _current_app.reset(token)
    a.reset()


# ==================== VAL-MIG-CONF: Settings Migration ====================


class TestMigrationConfSettings:
    def test_mig_conf_test_command(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert s.test_command == "uv run mtest"

    def test_mig_conf_vcs(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert s.vcs == "jj"

    def test_mig_conf_min_edits_for_tasks(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert s.min_edits_for_tasks == 3

    def test_mig_conf_task_drift_threshold(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert s.task_drift_threshold == 8

    def test_mig_conf_override_token(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert s.override_token == "REMAINING_TASKS_ACKNOWLEDGED"

    def test_mig_conf_exploration_agents(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert len(s.exploration_agents) >= 10

    def test_mig_conf_guarded_cleanup_files(self) -> None:
        from hooks.conf import BioqaSettings

        s = BioqaSettings()
        assert "code-issues.jsonl" in s.guarded_cleanup_files
        assert "test-issues.jsonl" in s.guarded_cleanup_files

    def test_mig_conf_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hooks.conf import BioqaSettings

        monkeypatch.setenv("HOOKS_TEST_COMMAND", "pytest")
        s = BioqaSettings()
        assert s.test_command == "pytest"

    def test_mig_conf_env_override_vcs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hooks.conf import BioqaSettings

        monkeypatch.setenv("HOOKS_VCS", "git")
        s = BioqaSettings()
        assert s.vcs == "git"

    def test_mig_conf_env_override_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hooks.conf import BioqaSettings

        monkeypatch.setenv("HOOKS_MIN_EDITS_FOR_TASKS", "5")
        s = BioqaSettings()
        assert s.min_edits_for_tasks == 5


# ==================== VAL-MIG-MIXIN: File Mixins ====================


class TestMigrationFileMixins:
    def test_mig_mixin_is_infra_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("/repo/tests/util/modal/plugin.py"))
        mixin = BioqaFileMixin()
        assert mixin.is_infra(f)

    def test_mig_mixin_is_infra_false(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("/repo/bioqa/core.py"))
        mixin = BioqaFileMixin()
        assert not mixin.is_infra(f)

    def test_mig_mixin_is_plan_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("docs/plans/feature.md"))
        mixin = BioqaFileMixin()
        assert mixin.is_plan(f)

    def test_mig_mixin_is_plan_specs(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("docs/specs/api.md"))
        mixin = BioqaFileMixin()
        assert mixin.is_plan(f)

    def test_mig_mixin_is_plan_false(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("/repo/docs/README.md"))
        mixin = BioqaFileMixin()
        assert not mixin.is_plan(f)

    def test_mig_mixin_is_task_json_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path(".context/tasks/task-001.json"))
        mixin = BioqaFileMixin()
        assert mixin.is_task_json(f)

    def test_mig_mixin_is_task_json_false(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path(".context/outputs/result.json"))
        mixin = BioqaFileMixin()
        assert not mixin.is_task_json(f)

    def test_mig_mixin_is_guarded_cleanup_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        for name in ["code-issues.jsonl", "test-issues.jsonl", "arch-issues.jsonl", "correctness-fixes.jsonl"]:
            f = File(path=Path(f".context/cleanup/{name}"))
            mixin = BioqaFileMixin()
            assert mixin.is_guarded_cleanup(f), f"{name} should be guarded"

    def test_mig_mixin_is_guarded_cleanup_false(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path(".context/cleanup/output.txt"))
        mixin = BioqaFileMixin()
        assert not mixin.is_guarded_cleanup(f)

    def test_mig_mixin_is_internal_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path(".claude/hooks/src/commands.py"))
        mixin = BioqaFileMixin()
        assert mixin.is_internal(f)

    def test_mig_mixin_is_internal_false(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("bioqa/core.py"))
        mixin = BioqaFileMixin()
        assert not mixin.is_internal(f)

    def test_mig_mixin_is_www_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("www/src/page.tsx"))
        mixin = BioqaFileMixin()
        assert mixin.is_www(f)

    def test_mig_mixin_is_www_false(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("bioqa/core.py"))
        mixin = BioqaFileMixin()
        assert not mixin.is_www(f)

    def test_mig_mixin_is_source_true(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("bioqa/core.py"))
        mixin = BioqaFileMixin()
        assert mixin.is_source(f)

    def test_mig_mixin_is_source_false_test(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("tests/test_core.py"))
        mixin = BioqaFileMixin()
        assert not mixin.is_source(f)

    def test_mig_mixin_is_source_false_infra(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("/repo/tests/util/modal/plugin.py"))
        mixin = BioqaFileMixin()
        assert not mixin.is_source(f)

    def test_mig_mixin_is_source_false_non_py(self) -> None:
        from hooks.mixins import BioqaFileMixin

        f = File(path=Path("README.md"))
        mixin = BioqaFileMixin()
        assert not mixin.is_source(f)


class TestMigrationFileMixinApplied:
    def test_mig_mixin_applied_to_file(self) -> None:
        from hooks.mixins import BioqaFileMixin, apply_file_mixin, cleanup_mixins

        apply_file_mixin(BioqaFileMixin)
        try:
            f = File(path=Path("/repo/tests/util/modal/plugin.py"))
            assert f.is_infra  # type: ignore[attr-defined]
            f2 = File(path=Path("/repo/bioqa/core.py"))
            assert f2.is_source  # type: ignore[attr-defined]
            assert not f2.is_infra  # type: ignore[attr-defined]
        finally:
            cleanup_mixins()

    def test_mig_mixin_file_proxy_path_methods(self) -> None:
        f = File(path=Path("/tmp/test.py"))
        assert f.suffix == ".py"
        assert f.name == "test.py"
        assert f.parent == Path("/tmp")


# ==================== VAL-MIG-MIXIN: Transcript Mixins ====================


class TestMigrationTranscriptMixins:
    def _make_transcript(self, tool_uses: list[dict[str, Any]]) -> Transcript:
        messages: list[dict[str, Any]] = []
        for tu in tool_uses:
            messages.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": tu["name"],
                                "input": tu.get("input", {}),
                                "id": tu.get("id", "tu_1"),
                            }
                        ]
                    },
                }
            )
        return Transcript.from_messages(messages)

    def test_mig_mixin_project_edit_count(self) -> None:
        from hooks.mixins import BioqaTranscriptMixin

        t = self._make_transcript(
            [
                {
                    "name": "Edit",
                    "input": {"file_path": "bioqa/core.py", "old_string": "a", "new_string": "b"},
                    "id": "t1",
                },
                {"name": "Write", "input": {"file_path": "www/index.html", "content": "x"}, "id": "t2"},
                {"name": "Edit", "input": {"file_path": "README.md", "old_string": "a", "new_string": "b"}, "id": "t3"},
            ]
        )
        mixin = BioqaTranscriptMixin()
        assert mixin.project_edit_count(t) == 2

    def test_mig_mixin_task_stats(self) -> None:
        from hooks.mixins import BioqaTranscriptMixin

        t = self._make_transcript(
            [
                {"name": "TaskCreate", "input": {"subject": "Fix bug"}, "id": "t1"},
                {"name": "TaskCreate", "input": {"subject": "Add tests"}, "id": "t2"},
                {"name": "TaskUpdate", "input": {"taskId": "task-1", "status": "completed"}, "id": "t3"},
            ]
        )
        mixin = BioqaTranscriptMixin()
        created, resolved = mixin.task_stats(t)
        assert created == 2
        assert resolved == 1

    def test_mig_mixin_has_exploration_agent(self) -> None:
        from hooks.mixins import BioqaTranscriptMixin

        t = self._make_transcript(
            [
                {"name": "Agent", "input": {"subagent_type": "web-analyzer"}, "id": "t1"},
            ]
        )
        mixin = BioqaTranscriptMixin()
        assert mixin.has_exploration_agent(t, ["web-analyzer", "search-specialist"])

    def test_mig_mixin_has_exploration_agent_false(self) -> None:
        from hooks.mixins import BioqaTranscriptMixin

        t = self._make_transcript(
            [
                {"name": "Agent", "input": {"subagent_type": "feature-implementer"}, "id": "t1"},
            ]
        )
        mixin = BioqaTranscriptMixin()
        assert not mixin.has_exploration_agent(t, ["web-analyzer", "search-specialist"])


# ==================== VAL-MIG-CMD: Commands Migration ====================


class TestMigrationCommands:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.commands")

    def _dispatch_pre(self, tool_name: str, tool_input: dict[str, Any]) -> list[Any]:
        ctx = make_ctx(self.tmp_path)
        evt = make_pre_tool_event(tool_name, tool_input, ctx)
        return [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]

    def _dispatch_post(self, tool_name: str, tool_input: dict[str, Any]) -> list[Any]:
        ctx = make_ctx(self.tmp_path)
        evt = make_post_tool_event(tool_name, tool_input, ctx)
        return [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]

    def test_mig_cmd_git_stash_blocked(self) -> None:
        results = self._dispatch_pre("Bash", {"command": "git stash"})
        assert any(r and r.action == Action.block for r in results)

    def test_mig_cmd_git_status_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git status"}, ctx)
        matching = self.app.get_matching_hooks(evt)
        block_results = [execute_hook(entry, evt, self.tmp_path) for entry in matching if entry.spec.block]
        assert not any(r and r.action == Action.block for r in block_results)

    def test_mig_cmd_jj_op_restore_blocked(self) -> None:
        results = self._dispatch_pre("Bash", {"command": "jj op restore abc123"})
        assert any(r and r.action == Action.block for r in results)

    def test_mig_cmd_jj_op_log_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "jj op log"}, ctx)
        matching = [h for h in self.app.get_matching_hooks(evt) if h.spec.block]
        assert not matching

    def test_mig_cmd_frontend_build_bun_blocked(self) -> None:
        results = self._dispatch_pre("Bash", {"command": "bun run build"})
        assert any(r and r.action == Action.block for r in results)

    def test_mig_cmd_frontend_build_vite_blocked(self) -> None:
        results = self._dispatch_pre("Bash", {"command": "vite build"})
        assert any(r and r.action == Action.block for r in results)

    def test_mig_cmd_frontend_build_tsc_blocked(self) -> None:
        results = self._dispatch_pre("Bash", {"command": "tsc --noEmit"})
        assert any(r and r.action == Action.block for r in results)

    def test_mig_cmd_grep_blocked(self) -> None:
        results = self._dispatch_pre("Bash", {"command": "grep -r 'pattern' src/"})
        assert any(r and r.action == Action.block for r in results)

    def test_mig_cmd_rg_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "rg pattern src/"}, ctx)
        matching = [h for h in self.app.get_matching_hooks(evt) if h.spec.block]
        assert not matching

    def test_mig_cmd_python_c_bioqa_warned(self) -> None:
        results = self._dispatch_post("Bash", {"command": 'python -c "import bioqa"'})
        assert any(r and r.action == Action.warn for r in results)

    def test_mig_cmd_python_c_print_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_post_tool_event("Bash", {"command": 'python -c "print(1)"'}, ctx)
        matching = self.app.get_matching_hooks(evt)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in matching]
        assert not any(r and r.action == Action.warn for r in results)


# ==================== VAL-MIG-STYLE: Style Migration ====================


class TestMigrationStyleDocstring:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_docstring_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": '    """Do something."""'},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "docstring" in r.message.lower() for r in results)

    def test_mig_style_docstring_skips_test(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "tests/test_core.py", "old_string": "", "new_string": '    """Do something."""'},
            ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        docstring_hooks = [h for h in matching if h.spec.message and "docstring" in h.spec.message.lower()]
        assert not docstring_hooks


class TestMigrationStyleDictComp:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_dict_comp_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": "{k: v for k, v in data.items()}"},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "dict" in r.message.lower() for r in results)

    def test_mig_style_dict_comp_skips_test(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "tests/test_core.py", "old_string": "", "new_string": "{k: v for k, v in data.items()}"},
            ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        dict_hooks = [h for h in matching if h.spec.message and "dict" in h.spec.message.lower()]
        assert not dict_hooks


class TestMigrationStyleUnderscoreLint:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_underscore_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        content = "class _MyClass:\n    pass\n"
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": content},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "_MyClass" in (r.message or "") for r in results)

    def test_mig_style_underscore_allows_dunder(self) -> None:
        ctx = make_ctx(self.tmp_path)
        content = "class __Meta:\n    pass\n"
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": content},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        underscore_results = [r for r in results if r and r.message and "underscore" in r.message.lower()]
        assert not underscore_results


class TestMigrationStyleNestedImports:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_nested_import_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        content = "if True:\n    import os\n"
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": content},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "import" in (r.message or "").lower() for r in results)

    def test_mig_style_type_checking_import_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path)
        content = "if TYPE_CHECKING:\n    import os\n"
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": content},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        nested_import_results = [
            r for r in results if r and r.message and "nested" in r.message.lower() and "import" in r.message.lower()
        ]
        assert not nested_import_results


class TestMigrationStyleZipStrict:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_zip_without_strict_warns(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        source = "result = list(zip(a, b))\n"
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": source},
            ctx,
        )
        results = [execute_hook(entry, evt, tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "zip" in (r.message or "").lower() for r in results)

    def test_mig_style_zip_with_strict_allowed(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        source = "result = list(zip(a, b, strict=True))\n"
        evt = make_post_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": source},
            ctx,
        )
        results = [execute_hook(entry, evt, tmp_path) for entry in self.app.get_matching_hooks(evt)]
        zip_results = [
            r for r in results if r and r.message and "zip" in r.message.lower() and "strict" in r.message.lower()
        ]
        assert not zip_results


class TestMigrationStylePromptNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_prompt_nudge_fires(self) -> None:
        ctx = make_ctx(self.tmp_path)
        content = 'def prompt(self):\n    body = "Hello"\n'
        evt = make_pre_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": content},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "prompt" in (r.message or "").lower() for r in results)

    def test_mig_style_prompt_nudge_skips_test(self) -> None:
        ctx = make_ctx(self.tmp_path)
        content = 'def prompt(self):\n    body = "Hello"\n'
        evt = make_pre_tool_event(
            "Edit",
            {"file_path": "tests/test_core.py", "old_string": "", "new_string": content},
            ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        prompt_hooks = [h for h in matching if h.handler and "prompt" in h.name.lower()]
        assert not prompt_hooks

    def test_mig_style_prompt_nudge_skips_after_read(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=True)
        content = 'def prompt(self):\n    body = "Hello"\n'
        evt = make_pre_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": content},
            ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in matching]
        prompt_results = [
            r for r in results if r and "prompt" in (r.message or "").lower() and "llm" in (r.message or "").lower()
        ]
        assert not prompt_results


class TestMigrationStyleReviewGate:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_review_gate_blocks_on_py_edits(self) -> None:
        ctx = make_ctx(self.tmp_path)
        py_file = File(path=Path("/repo/bioqa/util/core.py"))
        ctx.transcript.extract_files = MagicMock(return_value=[py_file])
        ctx.t = ctx.transcript
        evt = StopEvent(_raw={}, ctx=ctx)
        matching = self.app.get_matching_hooks(evt)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in matching]
        assert any(r and r.action == Action.block and "style" in (r.message or "").lower() for r in results)

    def test_mig_style_review_gate_allows_without_edits(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.extract_files = MagicMock(return_value=[])
        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        style_blocks = [r for r in results if r and r.action == Action.block and "style" in (r.message or "").lower()]
        assert not style_blocks


class TestMigrationStyleguideDetailGate:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_detail_gate_blocks_without_detail_read(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(side_effect=lambda p: p == "STYLEGUIDE.md")
        ctx.transcript.extract_files = MagicMock(return_value=[])
        evt = make_pre_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": "x = 1"},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "styleguide" in (r.message or "").lower() for r in results)

    def test_mig_style_detail_gate_allows_after_detail_read(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=True)
        ctx.transcript.extract_files = MagicMock(return_value=[File(path=Path("docs/styleguide/functional.md"))])
        evt = make_pre_tool_event(
            "Edit",
            {"file_path": "bioqa/core.py", "old_string": "", "new_string": "x = 1"},
            ctx,
        )
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        detail_blocks = [
            r for r in results if r and r.action == Action.block and "styleguide" in (r.message or "").lower()
        ]
        assert not detail_blocks


class TestMigrationStylePreExistingNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_pre_existing_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Pre-existing, not caused by my changes."},
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        raw = {"tool_name": "Edit", "tool_input": EDIT_INPUT}
        evt = PostToolUseEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "pre-existing" in (r.message or "").lower() for r in results)

    def test_mig_style_pre_existing_allows_fixing(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I found an issue and will fix it now."},
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        raw = {"tool_name": "Edit", "tool_input": EDIT_INPUT}
        evt = PostToolUseEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        pre_existing_results = [r for r in results if r and "pre-existing" in (r.message or "").lower()]
        assert not pre_existing_results

    def test_mig_style_pre_existing_suppressed_by_pyright(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Pre-existing pyright type error, not caused by my changes."},
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        raw = {"tool_name": "Edit", "tool_input": EDIT_INPUT}
        evt = PostToolUseEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        pre_existing_results = [r for r in results if r and "pre-existing" in (r.message or "").lower()]
        assert not pre_existing_results


class TestMigrationStyleObserveInfer:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.style")

    def test_mig_style_observe_infer_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The `raw_input` for PostToolUseFailure should contain "
                                    "`tool_error`. Let me check what data the hook system provides"
                                ),
                            },
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        raw = {"tool_name": "Edit", "tool_input": EDIT_INPUT}
        evt = PostToolUseEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "infer" in (r.message or "").lower() for r in results)

    def test_mig_style_observe_infer_allows_evidence(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "From the trace, I can see the response "
                                    "contains a `results` field with the data we need"
                                ),
                            },
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        raw = {"tool_name": "Edit", "tool_input": EDIT_INPUT}
        evt = PostToolUseEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(entry, evt, self.tmp_path) for entry in self.app.get_matching_hooks(evt)]
        observe_results = [r for r in results if r and "infer" in (r.message or "").lower()]
        assert not observe_results


# ==================== read_json Utility ====================


class TestMigrationReadJson:
    def test_mig_read_json_valid_file(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "data.json"
        p.write_text('{"key": "value"}')
        result = read_json(p)
        assert result == {"key": "value"}

    def test_mig_read_json_missing_file(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "missing.json"
        result = read_json(p)
        assert result is None

    def test_mig_read_json_missing_file_with_default(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "missing.json"
        result = read_json(p, {})
        assert result == {}

    def test_mig_read_json_malformed(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "bad.json"
        p.write_text("not json")
        result = read_json(p)
        assert result is None

    def test_mig_read_json_malformed_with_default(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "bad.json"
        p.write_text("not json")
        result = read_json(p, {})
        assert result == {}
