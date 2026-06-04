from __future__ import annotations

from captain_hook import Event, FilePath, audit

audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)

audit(
    Event.PostToolUse,
    log_dir="logs/hooks",
    filename=lambda d: f"{d:%Y-%m-%dT%H}.jsonl",
    fields=lambda evt: {
        "event": evt.event_name.name,
        "tool": evt.tool_name,
        "agent": evt.agent_type,
        "file": str(evt.file.path) if evt.file else None,
    },
    only_if=[FilePath("**/*.py")],
)
