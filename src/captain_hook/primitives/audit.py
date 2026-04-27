from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from captain_hook.app import on
from captain_hook.types import Event, HookResult, TCondition

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent


def session_id_for(evt: BaseHookEvent) -> str | None:
    """Return a 12-char sha256 prefix of the transcript path, or ``None`` if unavailable."""
    return sha256(str(p).encode()).hexdigest()[:12] if (p := evt.ctx.t.path) else None


def default_log_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())) / ".context" / "hook-logs"


def default_fields(evt: BaseHookEvent) -> dict[str, Any]:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "event": evt.event_name.name,
        "tool": evt.tool_name,
        "file": str(evt.file.path) if evt.file else None,
        "session_id": session_id_for(evt),
    }


def audit(
    events: Event = Event.PreToolUse | Event.PostToolUse | Event.Stop,
    *,
    log_dir: Path | str | None = None,
    filename: Callable[[datetime], str] = lambda d: f"{d:%Y-%m-%d}.jsonl",
    fields: Callable[[BaseHookEvent], dict[str, Any]] = default_fields,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
) -> None:
    """Register a hook that appends one JSONL record per matching event.

    Each matching event writes a single line to ``<log_dir>/<filename(now)>``.
    Default fields are ``ts``, ``event``, ``tool``, ``file``, and ``session_id``
    (a 12-char sha256 prefix of the transcript path).

    Example:
        >>> from captain_hook import audit, Event
        >>> audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)

    Args:
        events: Event mask to audit. Defaults to PreToolUse | PostToolUse | Stop.
        log_dir: Output directory. Defaults to ``$CLAUDE_PROJECT_DIR/.context/hook-logs``.
        filename: ``(datetime) -> str`` mapping a timestamp to a filename.
        fields: ``(evt) -> dict`` for the per-record payload.
        only_if: Conditions that must match for the event to be recorded.
        skip_if: Conditions that, if matched, suppress recording.
    """
    resolved_dir = Path(log_dir) if log_dir else default_log_dir()

    @on(events, only_if=only_if, skip_if=skip_if)
    def audit_event(evt: BaseHookEvent) -> HookResult | None:
        resolved_dir.mkdir(parents=True, exist_ok=True)
        with (resolved_dir / filename(datetime.now(UTC))).open("a") as f:
            f.write(json.dumps(fields(evt)) + "\n")
        return None
