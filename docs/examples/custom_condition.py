from __future__ import annotations

from dataclasses import dataclass

from captain_hook import (
    Allow,
    BaseHookEvent,
    CustomCondition,
    Event,
    HookResult,
    InlineTests,
    Input,
    Warn,
    on,
)

LARGE_EDIT_TESTS: InlineTests = {
    Input(tool="Edit", content="\n".join(str(i) for i in range(20))): Warn(),
    Input(tool="Edit", content="one\ntwo\n"): Allow(),
}


@dataclass(frozen=True)
class LargeEdit(CustomCondition):
    max_lines: int = 50

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.content is not None and evt.content.count("\n") > self.max_lines


@on(Event.PreToolUse, only_if=[LargeEdit(max_lines=10)], tests=LARGE_EDIT_TESTS)
def warn_large_edit(evt: BaseHookEvent) -> HookResult | None:
    lines = evt.content.count("\n") if evt.content else 0
    return evt.warn(f"Large edit detected ({lines} lines) — consider splitting into smaller changes.")
