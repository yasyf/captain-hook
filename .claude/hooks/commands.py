from __future__ import annotations

import re

from captain_hook import Event, Tool, nudge
from captain_hook.events import PostToolUseFailureEvent

# Repo-specific overlay on top of the `general` pack (git stash / codex hooks
# come from the pack; only the captain-hook-specific guards live here).

nudge(
    "MISSING DEPENDENCY: Run `uv sync --extra dev` (or `uv pip install <package>`) to fix this. "
    "Do NOT make imports lazy, remove the importing code, or restructure "
    "code to avoid the import.",
    events=Event.PostToolUseFailure,
    only_if=[Tool("Bash")],
    when=lambda evt: (
        isinstance(evt, PostToolUseFailureEvent)
        and bool(re.search(r"ModuleNotFoundError|ImportError: (?:cannot import|No module named)", evt.error))
    ),
    max_fires=2,
)
