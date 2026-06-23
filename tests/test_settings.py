from __future__ import annotations

import types
from typing import Any

import pytest
from pydantic_settings import BaseSettings

from captain_hook.app import _state
from captain_hook.loader import discover_hooks


def make_module(name: str, attrs: dict[str, Any], annotations: dict[str, type] | None = None) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    if annotations:
        mod.__annotations__ = annotations
    return mod


class TestHooksSettingsBase:
    def test_hooks_settings_works_as_basesettings(self) -> None:
        from captain_hook.settings import HooksSettings

        settings = HooksSettings()
        assert isinstance(settings, BaseSettings)
        assert issubclass(HooksSettings, BaseSettings)

    def test_hooks_settings_subclassable(self) -> None:
        from captain_hook.settings import HooksSettings

        class MySettings(HooksSettings):
            debug: bool = False

        s = MySettings()
        assert s.debug is False

    def test_planning_agents_default_includes_explore_and_plan(self) -> None:
        from captain_hook.settings import HooksSettings

        names = HooksSettings().planning_agents
        assert "Explore" in names
        assert "Plan" in names
        assert "web-analyzer" in names

    def test_planning_agents_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.settings import HooksSettings

        monkeypatch.setenv("HOOKS_PLANNING_AGENTS", '["only-one"]')
        assert HooksSettings().planning_agents == ["only-one"]


class TestAutoConfBuildSettings:
    def test_builds_from_module_vars(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"debug": False, "max_retries": 3})
        mod.__annotations__ = {"max_retries": int}
        settings = build_settings(mod)
        assert isinstance(settings, BaseSettings)
        assert settings.debug is False
        assert settings.max_retries == 3

    def test_multiple_fields(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module(
            "conf",
            {
                "name": "hello",
                "count": 5,
                "rate": 1.5,
                "enabled": True,
            },
        )
        settings = build_settings(mod)
        assert settings.name == "hello"
        assert settings.count == 5
        assert settings.rate == 1.5
        assert settings.enabled is True


class TestAutoConfTypeHints:
    def test_respects_type_hints(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"max_retries": 3}, annotations={"max_retries": int})
        settings = build_settings(mod)
        assert settings.max_retries == 3

    def test_annotated_primitives(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module(
            "conf",
            {
                "name": "hello",
                "count": 5,
                "rate": 1.5,
                "debug": False,
            },
            annotations={
                "name": str,
                "count": int,
                "rate": float,
                "debug": bool,
            },
        )
        settings = build_settings(mod)
        assert settings.name == "hello"
        assert settings.count == 5
        assert settings.rate == 1.5
        assert settings.debug is False

    def test_annotated_list(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"tags": ["a", "b"]}, annotations={"tags": list[str]})
        settings = build_settings(mod)
        assert settings.tags == ["a", "b"]


class TestAutoConfSkips:
    def test_skips_private_vars(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"_secret": "hidden", "visible": "shown"})
        settings = build_settings(mod)
        assert settings.visible == "shown"
        assert not hasattr(settings, "_secret")

    def test_skips_uppercase_constants(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"MAX_SIZE": 100, "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "MAX_SIZE")

    def test_skips_callables(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"helper": lambda x: x, "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "helper")

    def test_skips_module_imports(self) -> None:
        import os

        from captain_hook.settings import build_settings

        mod = make_module("conf", {"os": os, "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "os")

    def test_skips_dunder_attrs(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"__all__": ["x"], "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "__all__")

    def test_skips_dict_values(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"x": {"a": 1}, "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "x")

    def test_skips_set_values(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"y": {1, 2}, "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "y")

    def test_skips_none_values(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"x": None, "name": "test"})
        settings = build_settings(mod)
        assert settings.name == "test"
        assert not hasattr(settings, "x")


class TestAutoConfMutableDefaults:
    def test_list_defaults_independent(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"items": [1, 2, 3]})
        s1 = build_settings(mod)
        s2 = build_settings(mod)
        assert s1.items == [1, 2, 3]
        assert s2.items == [1, 2, 3]
        s1.items.append(4)
        assert s2.items == [1, 2, 3]

    def test_tuple_defaults_factory(self) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"coords": (1, 2)})
        settings = build_settings(mod)
        assert settings.coords == (1, 2)


class TestAutoConfEnvPrefix:
    def test_default_hooks_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"debug": False})
        monkeypatch.setenv("HOOKS_DEBUG", "true")
        settings = build_settings(mod)
        assert settings.debug is True

    def test_env_override_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.settings import build_settings

        mod = make_module("conf", {"port": 8080}, annotations={"port": int})
        monkeypatch.setenv("HOOKS_PORT", "9090")
        settings = build_settings(mod)
        assert settings.port == 9090


@pytest.mark.usefixtures("isolate_modules")
class TestSettingsFromApp:
    def test_app_settings_accessible(self, tmp_path: Any) -> None:

        d = tmp_path / "shooks_a"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text("my_setting = 42\n")
        discover_hooks(d)
        assert _state.settings is not None
        assert _state.settings.my_setting == 42

    def test_discover_hooks_loads_conf_and_builds_settings(self, tmp_path: Any) -> None:

        d = tmp_path / "shooks_b"
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "conf.py").write_text("test_command: str = 'uv run mtest'\nvcs = 'jj'\nmin_edits: int = 3\n")
        discover_hooks(d)
        settings = _state.settings
        assert settings is not None
        assert settings.test_command == "uv run mtest"
        assert settings.vcs == "jj"
        assert settings.min_edits == 3
