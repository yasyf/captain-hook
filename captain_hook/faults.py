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


def record(source: str, exc: BaseException, root: str | None = None) -> None:
    """Record one fault under ``source``, keyed by its own text so a per-event failure stays one line.

    ``root`` is the project the failure belongs to; only a session under that root ever reads it back.
    A record already on disk is left alone, so ``at`` is when the fault first appeared rather than
    when it last repeated. At :data:`MAX_RECORDS` the oldest record makes way, because a hook whose
    error text carries a changing path would otherwise fill the store and mask everything after it.
    Called from inside ``except`` blocks: an unwritable state dir is logged, never raised over the
    failure being reported.
    """
    error = f"{type(exc).__name__}: {exc}"
    key = sha256(f"{root}\n{source}\n{error}".encode()).hexdigest()[:16]
    path = faults_dir() / f"{key}.json"
    try:
        if path.exists():
            return
        while len(records := sorted(faults_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)) >= MAX_RECORDS:
            records[0].unlink(missing_ok=True)
        payload = {"source": source, "error": error, "root": root, "at": datetime.now(UTC).isoformat()}
        atomic_write(path, json.dumps(payload))
    except OSError:
        logger.opt(exception=True).debug("fault record failed")


def drain(root: str | None = None) -> list[str]:
    """Every fault this root may read, removing each record as it is read.

    A record owned by another project stays put: the text is a raw exception, and announcing it
    injects that project's paths into this session's model context.
    """
    lines: list[str] = []
    for path in sorted(faults_dir().glob("*.json")):
        if (data := read_json(path)) is None:
            path.unlink(missing_ok=True)
            continue
        if (owner := data.get("root")) is not None and owner != root:
            continue
        lines.append(f"{ANNOUNCE_PREFIX} {data['source']} — {data['error']} (first seen {data['at']})")
        path.unlink(missing_ok=True)
    return lines
