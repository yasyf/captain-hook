# Example: Auditing tool use
#
# Demonstrates how to use `audit` to append a JSONL record per matching
# event for later offline analysis.  By default the records contain
# timestamp, event name, tool name, file path, and a short session id
# derived from the transcript path.

from captain_hook import Event, audit

# Default: log Pre/Post tool use and Stop events to
# $CLAUDE_PROJECT_DIR/.context/hook-logs/<YYYY-MM-DD>.jsonl
audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)

# Custom destination + record shape: only log PostToolUse, write hourly
# files, and record only event/tool fields.
audit(
    Event.PostToolUse,
    log_dir="logs/hooks",
    filename=lambda d: f"{d:%Y-%m-%dT%H}.jsonl",
    fields=lambda evt: {"event": evt.event_name.name, "tool": evt.tool_name},
)
