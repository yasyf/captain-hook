from __future__ import annotations

from importlib import import_module


def __getattr__(name: str) -> object:
    from captain_hook.exports import EXPORTS

    if name == "__all__":
        globals()[name] = value = sorted(EXPORTS)
        return value
    if (target := EXPORTS.get(name)) is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(target)
    value = module if target == f"{__name__}.{name}" else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    from captain_hook.exports import EXPORTS

    return sorted(set(globals()) | set(EXPORTS) | {"__all__"})
