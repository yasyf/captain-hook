from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from captain_hook.app import (
    AsyncDecisionError,
    _state,
    get_matching_hooks,
    load_gitignore,
    on,
    reset,
)
from captain_hook.app import (
    hook as register_hook,
)
from captain_hook.loader import discover_hooks
from captain_hook.types import (
    Command,
    Event,
    TestFile,
    Tool,
)
from tests.helpers import make_pre_tool_event

pytestmark = pytest.mark.usefixtures("isolate_modules")

WARNING_NO = logger.level("WARNING").no


class TestHookDeclarative:
    def test_hook_registers_with_message(self) -> None:
        register_hook(Event.PreToolUse, message="warning text")
        assert len(_state.hooks) == 1
        assert _state.hooks[0].handler is None
        assert _state.hooks[0].spec.message == "warning text"

    def test_hook_requires_message(self) -> None:
        with pytest.raises(TypeError):
            register_hook(Event.PreToolUse)  # type: ignore[call-arg]


class TestOnDecorator:
    def test_on_registers_handler(self) -> None:
        def my_handler(evt: Any) -> None:
            return None

        decorated = on(Event.PreToolUse, only_if=[Tool("Bash")])(my_handler)
        assert decorated is my_handler
        assert len(_state.hooks) == 1
        assert _state.hooks[0].handler is my_handler

    def test_on_returns_original_function(self) -> None:
        def handler(evt: Any) -> None:
            return None

        assert on(Event.PreToolUse)(handler) is handler


class TestAsyncDecisionGuard:
    """async_=True on a decision event is rejected: Claude Code never awaits it, so the verdict is lost."""

    @pytest.mark.parametrize("event", [Event.PreToolUse, Event.Stop, Event.SubagentStop, Event.PermissionRequest])
    def test_hook_rejects_async_on_decision_event(self, event: Event) -> None:
        with pytest.raises(AsyncDecisionError, match="silently discarded"):
            register_hook(event, message="m", async_=True)
        assert _state.hooks == []  # nothing registered

    def test_on_rejects_async_on_decision_event(self) -> None:
        with pytest.raises(AsyncDecisionError, match="Stop"):

            @on(Event.Stop, async_=True)
            def handler(evt: Any) -> None:
                return None

        assert _state.hooks == []

    def test_rejects_async_when_a_combined_flag_includes_a_decision_event(self) -> None:
        # A hook subscribing several events at once still can't be async if ANY is decision-capable.
        with pytest.raises(AsyncDecisionError, match="Stop"):
            register_hook(Event.PostToolUse | Event.Stop, message="m", async_=True)

    def test_allows_async_on_non_decision_event(self) -> None:
        register_hook(Event.PostToolUse, message="m", async_=True)
        register_hook(Event.SessionStart, message="m", async_=True)
        assert len(_state.hooks) == 2  # both non-decision events register cleanly

    def test_allows_sync_hook_on_decision_event(self) -> None:
        register_hook(Event.Stop, message="m")  # sync is the correct way to gate a decision event
        assert len(_state.hooks) == 1


class TestRegistrationFields:
    def test_stores_all_hookspec_fields(self) -> None:
        # PostToolUse (not a decision event) so async_=True is valid alongside the tool conditions;
        # PreToolUse would trip the async-on-decision guard (see TestAsyncDecisionGuard).
        register_hook(
            Event.PostToolUse,
            only_if=[Tool("Bash"), Command(r"git")],
            skip_if=[TestFile()],
            message="test",
            block=True,
            advisory_on_deny=True,
            max_fires=3,
            respect_gitignore=False,
            async_=True,
        )
        spec = _state.hooks[0].spec
        assert spec.events == Event.PostToolUse
        assert spec.only_if == (Tool("Bash"), Command(r"git"))
        assert spec.skip_if == (TestFile(),)
        assert spec.message == "test"
        assert spec.block is True
        assert spec.advisory_on_deny is True
        assert spec.max_fires == 3
        assert spec.respect_gitignore is False
        assert spec.async_ is True


class TestRegisteredHookMetadata:
    def test_handler_has_name_and_source(self) -> None:
        @on(Event.PreToolUse)
        def my_handler(evt: Any) -> None:
            return None

        entry = _state.hooks[0]
        assert "my_handler" in entry.name
        assert entry.source_file.endswith(".py")

    def test_declarative_gets_stem_and_source_file(self) -> None:
        register_hook(Event.Stop, message="test")
        [entry] = _state.hooks
        assert re.fullmatch(r"[\w.]+:hook_[0-9a-f]{8}", entry.name)
        assert entry.source_file.endswith("test_app.py")

    def test_declarative_distinct_messages_get_distinct_names(self) -> None:
        register_hook(Event.PreToolUse, message="first message")
        register_hook(Event.PreToolUse, message="second message")
        first, second = _state.hooks
        assert first.name != second.name

    def test_declarative_same_message_gets_stable_name(self) -> None:
        register_hook(Event.PreToolUse, message="stable message")
        register_hook(Event.Stop, message="stable message")
        first, second = _state.hooks
        assert first.name == second.name


class TestDiscoverHooks:
    def test_imports_modules(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_a"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "my_hook.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='discovered')\n"
        )
        discover_hooks(d)
        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.message == "discovered"

    def test_loads_conf_first(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_b"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text("my_setting = 42\nanother_setting = 'hello'\n")
        (d / "hook_a.py").write_text(
            "from captain_hook.app import _state, hook\n"
            "from captain_hook.types import Event\n"
            "assert _state.settings is not None\n"
            "hook(Event.PreToolUse, message='after_conf')\n"
        )
        discover_hooks(d)
        assert _state.settings is not None
        assert hasattr(_state.settings, "my_setting")

    def test_no_conf_module_still_works(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_c"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "simple.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='no_conf')\n"
        )
        discover_hooks(d)
        assert _state.settings is None
        assert len(_state.hooks) > 0

    def test_empty_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_d"
        d.mkdir()
        (d / "__init__.py").write_text("")
        discover_hooks(d)
        assert len(_state.hooks) == 0

    def test_skips_test_modules(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_skiptests"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "real_hook.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='real')\n"
        )
        (d / "test_real_hook.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='should_not_load')\n"
        )
        (d / "conftest.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='conftest_should_not_load')\n"
        )
        discover_hooks(d)
        assert [h.spec.message for h in _state.hooks] == ["real"]

    def test_warns_and_continues_on_unloadable(self, tmp_path: Path, logcap: Any) -> None:
        d = tmp_path / "dhooks_unloadable"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "good_hook.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='good')\n"
        )
        (d / "bad_hook.py").write_text("raise RuntimeError('boom')\n")
        discover_hooks(d)
        assert [h.spec.message for h in _state.hooks] == ["good"]
        assert any("bad_hook" in r.message and r.levelno >= WARNING_NO for r in logcap.records)
        # The failure is also recorded so `capt-hook test` fails instead of passing green.
        assert any(
            "bad_hook" in e.source and isinstance(e.exc, RuntimeError) and e.pack is None for e in _state.load_errors
        )


class TestMultipleHooks:
    def test_accumulate(self) -> None:
        register_hook(Event.PreToolUse, message="first")
        register_hook(Event.PreToolUse, message="second")
        register_hook(Event.PreToolUse, message="third")
        assert len(_state.hooks) == 3


class TestPlanningAgentSkip:
    def test_subagent_stop_default_skips_planning_agent(self) -> None:
        from captain_hook.testing.helpers import mock_subagent_stop_event

        register_hook(Event.SubagentStop, message="workflow check")
        evt = mock_subagent_stop_event(agent_type="Explore")
        assert get_matching_hooks(evt) == []

    def test_subagent_stop_opt_out_runs_on_planning_agent(self) -> None:
        from captain_hook.testing.helpers import mock_subagent_stop_event

        register_hook(Event.SubagentStop, message="runs anyway", skip_planning_agents=False)
        evt = mock_subagent_stop_event(agent_type="Explore")
        assert len(get_matching_hooks(evt)) == 1

    def test_subagent_stop_runs_on_non_planning_agent(self) -> None:
        from captain_hook.testing.helpers import mock_subagent_stop_event

        register_hook(Event.SubagentStop, message="workflow check")
        evt = mock_subagent_stop_event(agent_type="cleanup")
        assert len(get_matching_hooks(evt)) == 1

    def test_subagent_start_default_skips_planning_agent(self) -> None:
        from captain_hook.testing.helpers import mock_subagent_start_event

        register_hook(Event.SubagentStart, message="start check")
        evt = mock_subagent_start_event(agent_type="Plan")
        assert get_matching_hooks(evt) == []

    def test_on_decorator_threads_skip_planning_agents(self) -> None:
        @on(Event.SubagentStop, skip_planning_agents=False)
        def handler(evt: Any) -> None:
            return None

        assert _state.hooks[0].spec.skip_planning_agents is False

    def test_default_spec_has_skip_planning_agents_true(self) -> None:
        register_hook(Event.SubagentStop, message="default")
        assert _state.hooks[0].spec.skip_planning_agents is True

    def test_blocking_hook_default_fires_on_planning_agent(self) -> None:
        from captain_hook.testing.helpers import mock_subagent_stop_event

        register_hook(Event.SubagentStop, message="enforce", block=True)
        evt = mock_subagent_stop_event(agent_type="general-purpose")
        assert len(get_matching_hooks(evt)) == 1

    def test_blocking_default_spec_has_skip_planning_agents_false(self) -> None:
        register_hook(Event.SubagentStop, message="enforce", block=True)
        assert _state.hooks[0].spec.skip_planning_agents is False

    def test_pre_tool_use_never_skipped(self) -> None:
        register_hook(Event.PreToolUse, message="tool check")
        evt = make_pre_tool_event(
            tool_name="Task",
            tool_input={"subagent_type": "Explore", "prompt": "go"},
        )
        assert len(get_matching_hooks(evt)) == 1

    def test_settings_override_planning_agents(self, tmp_path: Path) -> None:
        from captain_hook.testing.helpers import mock_subagent_stop_event

        d = tmp_path / "dhooks_planning"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text(
            "from captain_hook.settings import HooksSettings\nclass CustomSettings(HooksSettings):\n    pass\n"
        )
        (d / "h.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.SubagentStop, message='default')\n"
        )
        discover_hooks(d)
        assert _state.settings is not None
        _state.settings.planning_agents = ["my-agent"]  # type: ignore[attr-defined]
        assert get_matching_hooks(mock_subagent_stop_event(agent_type="my-agent")) == []
        assert len(get_matching_hooks(mock_subagent_stop_event(agent_type="Explore"))) == 1


class TestGetMatchingHooks:
    def test_filters_by_event(self) -> None:
        register_hook(Event.PreToolUse, message="pre_tool")
        register_hook(Event.Stop, message="stop")
        matching = get_matching_hooks(make_pre_tool_event("Edit"))
        assert len(matching) == 1
        assert matching[0].spec.message == "pre_tool"


class TestReset:
    def test_clears_all_state(self) -> None:
        register_hook(Event.PreToolUse, message="test")
        _state.settings = object()  # type: ignore[assignment]
        reset()
        assert len(_state.hooks) == 0
        assert _state.settings is None
        assert len(_state.gitignore_patterns) == 0
        assert _state.classifier is None

    def test_clears_cached_state(self) -> None:
        register_hook(Event.PreToolUse, message="test")
        assert len(_state.hooks) == 1
        reset()
        assert len(_state.hooks) == 0
        register_hook(Event.Stop, message="fresh")
        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.events == Event.Stop


class TestGitignore:
    def test_loading_and_filtering(self, tmp_path: Path) -> None:
        from captain_hook.app import is_gitignored

        (tmp_path / ".gitignore").write_text("node_modules\n*.pyc\n# comment\n\n")
        load_gitignore(tmp_path)
        assert is_gitignored("node_modules/foo.js")
        assert is_gitignored("cache.pyc")
        assert not is_gitignored("src/main.py")

    @pytest.mark.parametrize(
        ("respect_gitignore", "expected"),
        [
            pytest.param(True, 0, id="respect_gitignore_true_skips"),
            pytest.param(False, 1, id="respect_gitignore_false_matches"),
        ],
    )
    def test_respect_gitignore(self, tmp_path: Path, respect_gitignore: bool, expected: int) -> None:
        (tmp_path / ".gitignore").write_text("node_modules\n")
        load_gitignore(tmp_path)
        register_hook(Event.PreToolUse, message="check", respect_gitignore=respect_gitignore)
        evt = make_pre_tool_event(
            tool_name="Edit",
            tool_input={
                "file_path": "node_modules/pkg/index.js",
                "old_string": "",
                "new_string": "x",
            },
        )
        assert len(get_matching_hooks(evt)) == expected


class TestRepeatedDiscovery:
    def test_discover_then_reset_and_rediscover(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_repeat"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "my_hook.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='repeated')\n"
        )

        discover_hooks(d)
        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.message == "repeated"

        reset()
        discover_hooks(d)
        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.message == "repeated"

    def test_repeated_discovery_with_conf(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_repeat_conf"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text("my_setting = 99\n")
        (d / "hook_b.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.Stop, message='conf_repeat')\n"
        )

        discover_hooks(d)
        assert _state.settings is not None
        assert len(_state.hooks) == 1

        reset()
        discover_hooks(d)
        assert _state.settings is not None
        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.message == "conf_repeat"

    def test_repeated_discovery_with_handler(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_repeat_handler"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "handler_hook.py").write_text(
            "from captain_hook.app import on\n"
            "from captain_hook.types import Event\n"
            "@on(Event.PreToolUse)\n"
            "def my_handler(evt):\n"
            "    return None\n"
        )

        discover_hooks(d)
        assert len(_state.hooks) == 1
        assert _state.hooks[0].handler is not None

        reset()
        discover_hooks(d)
        assert len(_state.hooks) == 1
        assert _state.hooks[0].handler is not None


class TestMultiModuleInterImports:
    def test_transitive_import_during_discovery_no_duplicate(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_trans"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "alpha.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.beta import BETA_CONST\n"
            "hook(Event.PreToolUse, message='alpha_hook')\n"
        )
        (d / "beta.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "BETA_CONST = 'beta_val'\n"
            "hook(Event.Stop, message='beta_hook')\n"
        )

        discover_hooks(d)
        messages = sorted(h.spec.message or "" for h in _state.hooks)
        assert messages == ["alpha_hook", "beta_hook"], f"Got: {messages}"

    def test_sibling_imports_no_duplicate_registrations(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_multi"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "hooks_a.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "SHARED_VALUE = 'from_a'\n"
            "hook(Event.PreToolUse, message='hook_a')\n"
        )
        (d / "hooks_b.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.hooks_a import SHARED_VALUE\n"
            "hook(Event.Stop, message=f'hook_b_{SHARED_VALUE}')\n"
        )

        discover_hooks(d)
        messages = sorted(h.spec.message or "" for h in _state.hooks)
        assert messages == ["hook_a", "hook_b_from_a"], f"Got: {messages}"

    def test_repeated_discovery_with_inter_imports(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_multi_repeat"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "base.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "SHARED = 'shared'\n"
            "hook(Event.PreToolUse, message='base_hook')\n"
        )
        (d / "child.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.base import SHARED\n"
            "hook(Event.Stop, message=f'child_{SHARED}')\n"  # noqa: ISC003
        )

        discover_hooks(d)
        assert len(_state.hooks) == 2

        reset()
        discover_hooks(d)
        assert len(_state.hooks) == 2
        messages = sorted(h.spec.message or "" for h in _state.hooks)
        assert messages == ["base_hook", "child_shared"]

    def test_fresh_imports_not_reloaded(self, tmp_path: Path) -> None:
        d = tmp_path / "dhooks_fresh"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "helper.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            "hook(Event.PreToolUse, message='helper')\n"
        )
        (d / "user_mod.py").write_text(
            "from captain_hook.app import hook\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.helper import *\n"
            "hook(Event.Stop, message='user')\n"
        )

        discover_hooks(d)
        helper_count = sum(1 for h in _state.hooks if h.spec.message == "helper")
        assert helper_count == 1, f"helper registered {helper_count} times (expected 1)"
