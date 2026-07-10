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
def count_dispatch(evt: BaseHookEvent) -> HookResult | None:
    """Append one line per dispatch so a test can count how many siblings ran."""
    _tick()
    return None


@on(Event.PreToolUse, tests={Input(command="ls"): Allow()})
def count_pretooluse(evt: BaseHookEvent) -> HookResult | None:
    """PreToolUse counterpart — proves the guard exempts PreToolUse (both siblings dispatch)."""
    _tick()
    return None
