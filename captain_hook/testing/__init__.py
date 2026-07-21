from __future__ import annotations

from typing import TYPE_CHECKING, Any

from captain_hook.testing.types import (
    Allow,
    Ask,
    Block,
    FileFixture,
    InlineTests,
    Input,
    Rewrite,
    TranscriptFixture,
    Warn,
)

if TYPE_CHECKING:
    from captain_hook.testing.fixtures import T as T


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"T"})


def __getattr__(name: str) -> Any:
    if name == "T":
        from captain_hook.testing.fixtures import T

        globals()["T"] = T
        return T
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
