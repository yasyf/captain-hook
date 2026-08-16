"""Durable record of the failures no hook response can carry.

The daemon runs async dispatch on a worker thread, after the response Claude Code reads has
already gone back, and a hook handler's exception is caught so one bad hook cannot take the event
down with it. Both land in the daemon log and nowhere else, so a check that reads hook responses
reports a clean run over work that silently did not happen — that is how a live
``ModuleNotFoundError`` on every ``SessionStart`` survived a green bill of health. A recorded
fault is drained by the next session start and told to the user; repeats collapse, because the
record is keyed by the fault text itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from loguru import logger

from captain_hook.util.fs import atomic_write, read_json
from captain_hook.util.paths import resolve_state_dir

ANNOUNCE_PREFIX = "captain-hook fault:"
MAX_RECORDS = 32


def faults_dir() -> Path:
    return resolve_state_dir() / "faults"


def record(source: str, exc: BaseException) -> None:
    """Record one fault under ``source``, keyed by its own text so a per-event failure stays one line.

    The store is machine-wide and drains only at a session start, so the first :data:`MAX_RECORDS`
    distinct failures are kept and the rest dropped: an error text carrying a path or a tool name
    keys differently every event, and an announcement nobody can read is the silence this replaces.
    Called from inside ``except`` blocks, so an unwritable state dir is logged and dropped rather
    than replacing the failure being reported with a second one.
    """
    error = f"{type(exc).__name__}: {exc}"
    key = sha256(f"{source}\n{error}".encode()).hexdigest()[:16]
    path = faults_dir() / f"{key}.json"
    try:
        if not path.exists() and len(list(faults_dir().glob("*.json"))) >= MAX_RECORDS:
            return
        atomic_write(path, json.dumps({"source": source, "error": error, "at": datetime.now(UTC).isoformat()}))
    except OSError:
        logger.opt(exception=True).debug("fault record failed")


def drain() -> list[str]:
    """Every recorded fault as one announcement line, removing each record as it is read."""
    lines: list[str] = []
    for path in sorted(faults_dir().glob("*.json")):
        if (data := read_json(path)) is not None:
            lines.append(f"{ANNOUNCE_PREFIX} {data['source']} — {data['error']} (first seen {data['at']})")
        path.unlink(missing_ok=True)
    return lines
