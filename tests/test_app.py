from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from captain_hook.events import PreToolUseEvent, StopEvent
from captain_hook.types import (
    Command,
    Event,
    TestFile,
    Tool,
)


@pytest.fixture(autouse=True)
def _clean_imports():
    snapshot_modules = set(sys.modules.keys())
    snapshot_path = sys.path[:]
    yield
    for key in set(sys.modules.keys()) - snapshot_modules:
        del sys.modules[key]
    sys.path[:] = snapshot_path


def _pre_tool_event(
    tool_name: str = "Edit",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PreToolUseEvent:
    return PreToolUseEvent(
        _raw={"tool_name": tool_name, "tool_input": tool_input or {}},
        ctx=ctx,
    )


def _stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx)


class TestHookAppIndependence:
    def test_two_instances_independent(self) -> None:
        from captain_hook.app import HookApp

        app1 = HookApp()
        app2 = HookApp()
        app1.hook(Event.PreToolUse, message="from app1")
        assert len(app1.hooks) == 1
        assert len(app2.hooks) == 0


class TestHookDeclarative:
    def test_hook_registers_with_message(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        result = app.hook(Event.PreToolUse, message="warning text")
        assert result is None
        assert len(app.hooks) == 1
        assert app.hooks[0].handler is None
        assert app.hooks[0].spec.message == "warning text"

    def test_hook_requires_message(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        with pytest.raises(TypeError):
            app.hook(Event.PreToolUse)  # type: ignore[call-arg]


class TestOnDecorator:
    def test_on_registers_handler(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()

        def my_handler(evt: Any) -> None:
            return None

        decorated = app.on(Event.PreToolUse, only_if=[Tool("Bash")])(my_handler)
        assert decorated is my_handler
        assert len(app.hooks) == 1
        assert app.hooks[0].handler is my_handler

    def test_on_returns_original_function(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()

        def handler(evt: Any) -> None:
            return None

        assert app.on(Event.PreToolUse)(handler) is handler


class TestSplitAPIs:
    def test_hook_and_on_are_separate(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        assert hasattr(app, "hook")
        assert hasattr(app, "on")
        assert app.hook is not app.on
        assert callable(app.hook)
        assert callable(app.on)


class TestRegistrationFields:
    def test_stores_all_hookspec_fields(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        app.hook(
            Event.PreToolUse,
            only_if=[Tool("Bash"), Command(r"git")],
            skip_if=[TestFile()],
            message="test",
            block=True,
            max_fires=3,
            respect_gitignore=False,
            async_=True,
        )
        spec = app.hooks[0].spec
        assert spec.events == Event.PreToolUse
        assert spec.only_if == (Tool("Bash"), Command(r"git"))
        assert spec.skip_if == (TestFile(),)
        assert spec.message == "test"
        assert spec.block is True
        assert spec.max_fires == 3
        assert spec.respect_gitignore is False
        assert spec.async_ is True


class TestRegisteredHookMetadata:
    def test_handler_has_name_and_source(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()

        @app.on(Event.PreToolUse)
        def my_handler(evt: Any) -> None:
            return None

        hook = app.hooks[0]
        assert "my_handler" in hook.name
        assert hook.source_file.endswith(".py")

    def test_declarative_has_nonempty_name(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        app.hook(Event.Stop, message="test")
        assert app.hooks[0].name != ""


class TestDiscoverHooks:
    def test_imports_modules(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_a"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "my_hook.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "get_current_app().hook(Event.PreToolUse, message='discovered')\n"
        )
        app = HookApp()
        app.discover_hooks(d)
        assert len(app.hooks) == 1
        assert app.hooks[0].spec.message == "discovered"

    def test_loads_conf_first(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_b"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text("my_setting = 42\nanother_setting = 'hello'\n")
        (d / "hook_a.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "app = get_current_app()\n"
            "assert app.settings is not None\n"
            "app.hook(Event.PreToolUse, message='after_conf')\n"
        )
        app = HookApp()
        app.discover_hooks(d)
        assert app.settings is not None
        assert hasattr(app.settings, "my_setting")

    def test_no_conf_module_still_works(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_c"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "simple.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "get_current_app().hook(Event.PreToolUse, message='no_conf')\n"
        )
        app = HookApp()
        app.discover_hooks(d)
        assert app.settings is None
        assert len(app.hooks) > 0

    def test_empty_directory(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_d"
        d.mkdir()
        (d / "__init__.py").write_text("")
        app = HookApp()
        app.discover_hooks(d)
        assert len(app.hooks) == 0


class TestMultipleHooks:
    def test_accumulate(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        app.hook(Event.PreToolUse, message="first")
        app.hook(Event.PreToolUse, message="second")
        app.hook(Event.PreToolUse, message="third")
        assert len(app.hooks) == 3


class TestGetMatchingHooks:
    def test_filters_by_event(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        app.hook(Event.PreToolUse, message="pre_tool")
        app.hook(Event.Stop, message="stop")
        matching = app.get_matching_hooks(_pre_tool_event())
        assert len(matching) == 1
        assert matching[0].spec.message == "pre_tool"


class TestReset:
    def test_clears_all_state(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        app.hook(Event.PreToolUse, message="test")
        app.settings = {"key": "value"}
        app.reset()
        assert len(app.hooks) == 0
        assert app.settings is None
        assert len(app.gitignore_patterns) == 0
        assert app.classifier is None
        assert app.counter == 0

    def test_clears_cached_state(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        app.hook(Event.PreToolUse, message="test")
        assert len(app.hooks) == 1
        app.reset()
        assert len(app.hooks) == 0
        app.hook(Event.Stop, message="fresh")
        assert len(app.hooks) == 1
        assert app.hooks[0].spec.events == Event.Stop


class TestGitignore:
    def test_loading_and_filtering(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        (tmp_path / ".gitignore").write_text("node_modules\n*.pyc\n# comment\n\n")
        app = HookApp()
        app.load_gitignore(tmp_path)
        assert app.is_gitignored("node_modules/foo.js")
        assert app.is_gitignored("cache.pyc")
        assert not app.is_gitignored("src/main.py")

    def test_respect_gitignore_true_skips(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        (tmp_path / ".gitignore").write_text("node_modules\n")
        app = HookApp()
        app.load_gitignore(tmp_path)
        app.hook(Event.PreToolUse, message="check", respect_gitignore=True)
        evt = _pre_tool_event(
            tool_name="Edit",
            tool_input={
                "file_path": "node_modules/pkg/index.js",
                "old_string": "",
                "new_string": "x",
            },
        )
        assert len(app.get_matching_hooks(evt)) == 0

    def test_respect_gitignore_false_matches(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        (tmp_path / ".gitignore").write_text("node_modules\n")
        app = HookApp()
        app.load_gitignore(tmp_path)
        app.hook(Event.PreToolUse, message="check", respect_gitignore=False)
        evt = _pre_tool_event(
            tool_name="Edit",
            tool_input={
                "file_path": "node_modules/pkg/index.js",
                "old_string": "",
                "new_string": "x",
            },
        )
        assert len(app.get_matching_hooks(evt)) == 1


class TestRegister:
    def test_returns_decorator_without_message(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        dec = app.register(events=Event.PreToolUse)
        assert callable(dec)
        assert len(app.hooks) == 0

    def test_registers_when_decorator_applied(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        dec = app.register(events=Event.PreToolUse)

        @dec
        def handler(evt: Any) -> None:
            return None

        assert len(app.hooks) == 1
        assert app.hooks[0].handler is handler

    def test_registers_declarative_with_message(self) -> None:
        from captain_hook.app import HookApp

        app = HookApp()
        result = app.register(events=Event.PreToolUse, message="warn")
        assert result is None
        assert len(app.hooks) == 1
        assert app.hooks[0].spec.message == "warn"


class TestRepeatedDiscovery:
    def test_two_apps_discover_same_directory(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_repeat"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "my_hook.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "get_current_app().hook(Event.PreToolUse, message='repeated')\n"
        )

        app1 = HookApp()
        app1.discover_hooks(d)
        assert len(app1.hooks) == 1
        assert app1.hooks[0].spec.message == "repeated"

        app2 = HookApp()
        app2.discover_hooks(d)
        assert len(app2.hooks) == 1
        assert app2.hooks[0].spec.message == "repeated"

    def test_reload_does_not_affect_first_app(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_reload_safe"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "hook_a.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "get_current_app().hook(Event.PreToolUse, message='safe')\n"
        )

        app1 = HookApp()
        app1.discover_hooks(d)
        original_count = len(app1.hooks)

        app2 = HookApp()
        app2.discover_hooks(d)

        assert len(app1.hooks) == original_count
        assert len(app2.hooks) == 1

    def test_repeated_discovery_with_conf(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_repeat_conf"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text("my_setting = 99\n")
        (d / "hook_b.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "get_current_app().hook(Event.Stop, message='conf_repeat')\n"
        )

        app1 = HookApp()
        app1.discover_hooks(d)
        assert app1.settings is not None
        assert len(app1.hooks) == 1

        app2 = HookApp()
        app2.discover_hooks(d)
        assert app2.settings is not None
        assert len(app2.hooks) == 1
        assert app2.hooks[0].spec.message == "conf_repeat"

    def test_repeated_discovery_with_handler(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_repeat_handler"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "handler_hook.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "app = get_current_app()\n"
            "@app.on(Event.PreToolUse)\n"
            "def my_handler(evt):\n"
            "    return None\n"
        )

        app1 = HookApp()
        app1.discover_hooks(d)
        assert len(app1.hooks) == 1
        assert app1.hooks[0].handler is not None

        app2 = HookApp()
        app2.discover_hooks(d)
        assert len(app2.hooks) == 1
        assert app2.hooks[0].handler is not None


class TestMultiModuleInterImports:
    def test_transitive_import_during_discovery_no_duplicate(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_trans"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "alpha.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.beta import BETA_CONST\n"
            "get_current_app().hook(Event.PreToolUse, message='alpha_hook')\n"
        )
        (d / "beta.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "BETA_CONST = 'beta_val'\n"
            "get_current_app().hook(Event.Stop, message='beta_hook')\n"
        )

        app = HookApp()
        app.discover_hooks(d)
        messages = sorted(h.spec.message or "" for h in app.hooks)
        assert messages == ["alpha_hook", "beta_hook"], f"Got: {messages}"

    def test_sibling_imports_no_duplicate_registrations(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_multi"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "hooks_a.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "SHARED_VALUE = 'from_a'\n"
            "get_current_app().hook(Event.PreToolUse, message='hook_a')\n"
        )
        (d / "hooks_b.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.hooks_a import SHARED_VALUE\n"
            "get_current_app().hook(Event.Stop, message=f'hook_b_{SHARED_VALUE}')\n"
        )

        app = HookApp()
        app.discover_hooks(d)
        messages = sorted(h.spec.message or "" for h in app.hooks)
        assert messages == ["hook_a", "hook_b_from_a"], f"Got: {messages}"

    def test_repeated_discovery_with_inter_imports(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_multi_repeat"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "base.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "SHARED = 'shared'\n"
            "get_current_app().hook(Event.PreToolUse, message='base_hook')\n"
        )
        (d / "child.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.base import SHARED\n"
            "get_current_app().hook(Event.Stop, message=f'child_{SHARED}')\n"  # noqa: ISC003
        )

        app1 = HookApp()
        app1.discover_hooks(d)
        assert len(app1.hooks) == 2

        app2 = HookApp()
        app2.discover_hooks(d)
        assert len(app2.hooks) == 2
        messages = sorted(h.spec.message or "" for h in app2.hooks)
        assert messages == ["base_hook", "child_shared"]

    def test_fresh_imports_not_reloaded(self, tmp_path: Path) -> None:
        from captain_hook.app import HookApp

        d = tmp_path / "dhooks_fresh"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "helper.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            "get_current_app().hook(Event.PreToolUse, message='helper')\n"
        )
        (d / "user_mod.py").write_text(
            "from captain_hook.app import get_current_app\n"
            "from captain_hook.types import Event\n"
            f"from {d.name}.helper import *\n"
            "get_current_app().hook(Event.Stop, message='user')\n"
        )

        app = HookApp()
        app.discover_hooks(d)
        helper_count = sum(1 for h in app.hooks if h.spec.message == "helper")
        assert helper_count == 1, f"helper registered {helper_count} times (expected 1)"
