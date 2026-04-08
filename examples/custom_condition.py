# Example: Custom condition via the CustomCondition protocol
#
# Demonstrates how to implement a custom condition class that
# satisfies the `CustomCondition` protocol.  Custom conditions
# let you write arbitrary matching logic beyond the built-in
# condition types (Tool, FilePath, Command, etc.).
#
# This example also uses the `@app.on()` handler decorator pattern.

from captain_hook import (
    CustomCondition,
    Event,
    HookApp,
    HookResult,
    dispatch,
    mock_tool_event,
)
from captain_hook.events import BaseHookEvent


class LargeEdit(CustomCondition):
    def __init__(self, max_lines: int = 50) -> None:
        self.max_lines = max_lines

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.content is not None and evt.content.count("\n") > self.max_lines


app = HookApp()

@app.on(Event.PreToolUse, only_if=[LargeEdit(max_lines=10)])
def warn_large_edit(evt: BaseHookEvent) -> HookResult | None:
    line_count = evt.content.count("\n") if evt.content else 0
    return evt.warn(f"Large edit detected ({line_count} lines) — consider splitting into smaller changes.")


small_content = "line1\nline2\nline3\n"
evt_small = mock_tool_event("Write", event=Event.PreToolUse, file="small.py", content=small_content)
result_small = dispatch(app, Event.PreToolUse, evt_small)
assert result_small is None

large_content = "\n".join(f"line {i}" for i in range(50))
evt_large = mock_tool_event("Write", event=Event.PreToolUse, file="large.py", content=large_content)
result_large = dispatch(app, Event.PreToolUse, evt_large)
assert result_large is not None
output = result_large["hookSpecificOutput"]
assert output["permissionDecision"] == "allow"
assert "Large edit detected" in output["additionalContext"]

print("✓ Custom condition fired for large edit, skipped for small edit")
