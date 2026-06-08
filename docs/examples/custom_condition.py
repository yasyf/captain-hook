from __future__ import annotations

from dataclasses import dataclass

from captain_hook import (
    Allow,
    BaseHookEvent,
    CustomCondition,
    Event,
    Input,
    Warn,
    nudge,
)


@dataclass(frozen=True)
class LargeEdit(CustomCondition):
    max_lines: int = 50

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.content is not None and evt.content.count("\n") > self.max_lines


nudge(
    "Large edit detected — consider splitting into smaller changes.",
    only_if=[LargeEdit(max_lines=10)],
    events=Event.PreToolUse,
    tests={
        Input(tool="Edit", content="\n".join(str(i) for i in range(20))): Warn(),
        Input(tool="Edit", content="one\ntwo\n"): Allow(),
    },
)
