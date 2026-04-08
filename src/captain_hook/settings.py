from __future__ import annotations

import types
from typing import Any, get_type_hints

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

INFERRABLE_PRIMITIVES = (str, int, float, bool)


class HooksSettings(BaseSettings):
    """Base settings class for hook configuration, backed by environment variables with ``HOOKS_`` prefix."""

    model_config = SettingsConfigDict(env_prefix="HOOKS_")


class AutoConf:
    """Automatic settings builder that infers a ``HooksSettings`` subclass from a conf module's attributes."""

    @staticmethod
    def should_skip(name: str, val: Any) -> bool:
        return name.startswith("_") or name.isupper() or callable(val) or isinstance(val, types.ModuleType)

    @staticmethod
    def find_settings_class(module: types.ModuleType) -> type[HooksSettings] | None:
        for val in vars(module).values():
            if isinstance(val, type) and issubclass(val, HooksSettings) and val is not HooksSettings:
                return val
        return None

    @staticmethod
    def build_settings(module: types.ModuleType, prefix: str = "HOOKS_") -> BaseSettings:
        if settings_cls := AutoConf.find_settings_class(module):
            return settings_cls()

        try:
            hints = get_type_hints(module)
        except Exception:
            hints = {}

        candidates = sorted(
            set(hints.keys()) | {k for k in vars(module) if not AutoConf.should_skip(k, getattr(module, k))}
        )

        fields: dict[str, tuple[type, Any]] = {}
        for name in candidates:
            val = getattr(module, name, None)
            if AutoConf.should_skip(name, val):
                continue
            if val is None or isinstance(val, (dict, set)):
                continue
            match name in hints, isinstance(val, list):
                case True, True:
                    fields[name] = (hints[name], Field(default_factory=lambda v=val: list(v)))
                case True, False:
                    fields[name] = (hints[name], Field(default=val))
                case False, True:
                    fields[name] = (list, Field(default_factory=lambda v=val: list(v)))
                case False, _ if isinstance(val, tuple):
                    fields[name] = (tuple, Field(default_factory=lambda v=val: tuple(v)))
                case False, _ if isinstance(val, INFERRABLE_PRIMITIVES):
                    fields[name] = (type(val), Field(default=val))
                case _:
                    pass

        return type(
            "AutoSettings",
            (HooksSettings,),
            {
                "__annotations__": {k: t for k, (t, _) in fields.items()},
                "model_config": SettingsConfigDict(env_prefix=prefix),
                **{k: fd for k, (_, fd) in fields.items()},
            },
        )()


def build_settings(module: types.ModuleType, prefix: str = "HOOKS_") -> BaseSettings:
    """Build a settings instance from a conf module, using an explicit ``HooksSettings`` subclass or auto-inferring fields."""
    return AutoConf.build_settings(module, prefix)
