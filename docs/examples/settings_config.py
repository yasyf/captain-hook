"""Enforce a project's configured test runner using typed per-project settings."""

from __future__ import annotations

from typing import cast

from captain_hook import (
    Allow,
    BaseHookEvent,
    Event,
    HookResult,
    HooksSettings,
    Input,
    Tool,
    on,
)


class ProjectSettings(HooksSettings):
    test_command: str = "pytest"
    require_tests_after_edit: bool = True
    excluded_dirs: tuple[str, ...] = ("vendor", "node_modules")


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash")],
    tests={
        Input(tool="Bash", command="ls -la"): Allow(),
        Input(tool="Bash", command="ruff check ."): Allow(),
    },
)
def enforce_test_command(evt: BaseHookEvent) -> HookResult | None:
    settings = cast(ProjectSettings, evt.ctx.c)
    if (cmd := evt.command).raw and cmd.q.runs("pytest") and settings.test_command != "pytest":
        return evt.block(f"BLOCKED: use the project's configured test runner instead. Run: {settings.test_command}")
    return None
