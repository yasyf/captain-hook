from __future__ import annotations

import re

from captain_hook import Event, Tool, nudge
from captain_hook.events import PostToolUseFailureEvent

nudge(
    "MISSING DEPENDENCY: run `go mod tidy` (or `go get <module>`) to resolve it. "
    "Do NOT make the import lazy, delete the importing code, or vendor by hand.",
    events=Event.PostToolUseFailure,
    only_if=[Tool("Bash")],
    when=lambda evt: (
        isinstance(evt, PostToolUseFailureEvent)
        and bool(
            re.search(
                r"no required module provides package|missing go\.sum entry|"
                r"cannot find module|updates to go\.mod needed",
                evt.error,
            )
        )
    ),
    max_fires=2,
)
