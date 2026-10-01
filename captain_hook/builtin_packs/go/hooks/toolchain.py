from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, Tool, Warn, nudge
from captain_hook.events import PostToolUseFailureEvent

nudge(
    "A Go module is missing. Run `go mod tidy`.",
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
    tests={
        Input(
            command="go build ./...",
            error="no required module provides package github.com/x/y; to add it:\n\tgo get github.com/x/y",
        ): Warn(),
        Input(command="go build ./...", error="undefined: foo"): Allow(),
    },
)
