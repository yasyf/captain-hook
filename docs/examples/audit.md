# Audit Logging

You want a per-day record of every tool the agent invoked, what file it
touched, and which session it came from -- so you can investigate
incidents after the fact, build a usage dashboard, or just spot-check
what the agent has been doing.

## The simplest case

```python
from captain_hook import audit, Event

audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)
```

That's the entire hook. Each matching event appends one JSON line to
`$CLAUDE_PROJECT_DIR/.context/hook-logs/<YYYY-MM-DD>.jsonl`:

```json
{"ts": "2026-04-27T10:23:11.413927+00:00", "event": "PreToolUse", "tool": "Bash", "file": null, "session_id": "8e1f7a0c4c2f"}
{"ts": "2026-04-27T10:23:13.882104+00:00", "event": "PostToolUse", "tool": "Edit", "file": "/repo/src/app.py", "session_id": "8e1f7a0c4c2f"}
{"ts": "2026-04-27T10:24:02.117450+00:00", "event": "Stop", "tool": null, "file": null, "session_id": "8e1f7a0c4c2f"}
```

The `session_id` is the first 12 hex chars of the SHA256 of the
transcript path, so all events from the same Claude Code session share
an id without exposing the path.

## Customizing the record

Pass a `fields` callable to control the per-record payload:

```python
from captain_hook import audit, Event

audit(
    Event.PostToolUse,
    fields=lambda evt: {
        "event": evt.event_name.name,
        "tool": evt.tool_name,
        "agent": evt.agent_type,
        "file": str(evt.file.path) if evt.file else None,
    },
)
```

## Hourly rotation

```python
from captain_hook import audit, Event

audit(
    Event.PreToolUse | Event.PostToolUse,
    filename=lambda d: f"{d:%Y-%m-%dT%H}.jsonl",
)
```

## Filtering events

Use `only_if` / `skip_if` exactly as on other primitives:

```python
from captain_hook import audit, Event, FilePath

audit(
    Event.PostToolUse,
    only_if=[FilePath("**/*.py")],
)
```
