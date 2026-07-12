from __future__ import annotations

import os
from pathlib import Path

from captain_hook import BaseHookEvent, Event, HookResult, on
from captain_hook.testing import Allow, Input


def _tick() -> None:
    if counter := os.environ.get("CAPT_HOOK_TEST_COUNTER"):
        with Path(counter).open("a") as f:
            f.write("x\n")


@on(Event.Stop, tests={Input(): Allow()})
def count_stop(evt: BaseHookEvent) -> HookResult | None:
    """Append one line per dispatch so a test can count how many siblings ran."""
    _tick()
    return None


@on(Event.SubagentStop, tests={Input(): Allow()})
def count_subagent_stop(evt: BaseHookEvent) -> HookResult | None:
    """SubagentStop counterpart — a decision-capable event the guard exempts."""
    _tick()
    return None


@on(Event.PreToolUse, tests={Input(command="ls"): Allow()})
def count_pretooluse(evt: BaseHookEvent) -> HookResult | None:
    """PreToolUse counterpart — a decision-capable event the guard exempts."""
    _tick()
    return None


@on(Event.PermissionRequest, tests={Input(command="ls"): Allow()})
def count_permission_request(evt: BaseHookEvent) -> HookResult | None:
    """PermissionRequest counterpart — a decision-capable event the guard exempts."""
    _tick()
    return None


@on(Event.PostToolUse, tests={Input(command="ls", output="done"): Allow()})
def count_posttooluse(evt: BaseHookEvent) -> HookResult | None:
    """PostToolUse counterpart — a side-effect event the guard collapses."""
    _tick()
    return None


@on(Event.UserPromptSubmit, tests={Input(prompt="continue"): Allow()})
def count_user_prompt_submit(evt: BaseHookEvent) -> HookResult | None:
    """UserPromptSubmit counterpart — a side-effect event the guard collapses."""
    _tick()
    return None
