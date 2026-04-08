# Example: Custom condition via the CustomCondition protocol
#
# Demonstrates how to implement a custom condition class that
# satisfies the `CustomCondition` protocol.  Custom conditions
# let you write arbitrary matching logic beyond the built-in
# condition types (Tool, FilePath, Command, etc.).

from dataclasses import dataclass

from captain_hook import CustomCondition, Event, HookResult, on
from captain_hook.events import BaseHookEvent


@dataclass
class LargeEdit(CustomCondition):
    """Matches when the event content exceeds a line threshold."""

    max_lines: int = 50

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.content is not None and evt.content.count("\n") > self.max_lines


# Register a handler that fires only when LargeEdit matches.
@on(Event.PreToolUse, only_if=[LargeEdit(max_lines=10)])
def warn_large_edit(evt: BaseHookEvent) -> HookResult | None:
    line_count = evt.content.count("\n") if evt.content else 0
    return evt.warn(f"Large edit detected ({line_count} lines) — consider splitting into smaller changes.")
