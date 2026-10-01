from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, Tool, Warn, nudge
from captain_hook.events import PostToolUseFailureEvent

nudge(
    "A Python dependency is missing. Run `uv sync --extra dev`.",
    events=Event.PostToolUseFailure,
    only_if=[Tool("Bash")],
    when=lambda evt: (
        isinstance(evt, PostToolUseFailureEvent)
        and bool(re.search(r"ModuleNotFoundError|ImportError: (?:cannot import|No module named)", evt.error))
    ),
    max_fires=2,
    tests={
        Input(command="uv run pytest", error="ModuleNotFoundError: No module named 'yaml'"): Warn(),
        Input(command="uv run pytest", error="AssertionError: 1 != 2"): Allow(),
    },
)
