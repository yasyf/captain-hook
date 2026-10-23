from __future__ import annotations

from captain_hook import Event, TouchedFile, nudge

nudge(
    "New Python code gets a STYLEGUIDE.md review before you stop. Review your diff against it and fix every violation.",
    only_if=[TouchedFile("**/captain_hook/**/*.py", subagents=True)],
    events=Event.Stop,
)
