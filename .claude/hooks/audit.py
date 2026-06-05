from __future__ import annotations

from captain_hook import Event, audit

audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)
