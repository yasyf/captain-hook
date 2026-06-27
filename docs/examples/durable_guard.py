"""Warn the first time a sensitive file is edited in this project — once, across every session."""

from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Deque,
    DurableState,
    Event,
    HookResult,
    Input,
    Tool,
    Warn,
    on,
)

SENSITIVE = ("migrations/", "secrets", ".env")


class WarnedPaths(DurableState, scope="project"):
    paths: Deque[256]


def is_sensitive(path: str) -> bool:
    return any(marker in path for marker in SENSITIVE)


@on(
    Event.PreToolUse,
    only_if=[Tool("Edit|Write")],
    tests={
        Input(tool="Edit", file=".env", content="API_KEY=x\n"): Warn(pattern="sensitive"),
        Input(tool="Edit", file="app/users.py", content="x = 1\n"): Allow(),
    },
)
def warn_once_per_project(evt: BaseHookEvent) -> HookResult | None:
    if not (fp := evt.file) or not is_sensitive(path := str(fp.path)):
        return None
    with WarnedPaths.mutate(evt) as state:
        if path in state.paths:
            return None
        state.paths.append(path)
    return evt.warn(f"Editing a sensitive file (`{path}`). Double-check before committing secrets.")
