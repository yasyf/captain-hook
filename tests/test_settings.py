from __future__ import annotations

import os
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
    @pytest.mark.parametrize(
        ("attrs", "skipped_key", "kept_field", "kept_value"),
        [
            pytest.param({"_secret": "hidden", "visible": "shown"}, "_secret", "visible", "shown", id="private_vars"),
            pytest.param({"MAX_SIZE": 100, "name": "test"}, "MAX_SIZE", "name", "test", id="uppercase_constants"),
            pytest.param({"helper": lambda x: x, "name": "test"}, "helper", "name", "test", id="callables"),
            pytest.param({"os": os, "name": "test"}, "os", "name", "test", id="module_imports"),
            pytest.param({"__all__": ["x"], "name": "test"}, "__all__", "name", "test", id="dunder_attrs"),
            pytest.param({"x": {"a": 1}, "name": "test"}, "x", "name", "test", id="dict_values"),
            pytest.param({"y": {1, 2}, "name": "test"}, "y", "name", "test", id="set_values"),
            pytest.param({"x": None, "name": "test"}, "x", "name", "test", id="none_values"),
        ],
    )
    def test_skips(self, attrs: dict[str, Any], skipped_key: str, kept_field: str, kept_value: Any) -> None:
        from captain_hook.settings import build_settings

        settings = build_settings(make_module("conf", attrs))
        assert getattr(settings, kept_field) == kept_value
        assert not hasattr(settings, skipped_key)


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
