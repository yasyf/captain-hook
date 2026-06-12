"""SQLite log of every hook fire, the source of truth that joins agent misfire complaints to the firing hook."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Self

from captain_hook.review.repo import repo_key
from captain_hook.session import session_hash
from captain_hook.settings import resolve_fire_log_enabled, resolve_fire_log_path

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult, RegisteredHook

BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS fires (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                REAL    NOT NULL,
  session_id        TEXT    NOT NULL,
  claude_session_id TEXT,
  repo_key          TEXT,
  hook_name         TEXT    NOT NULL,
  source_file       TEXT    NOT NULL,
  event             TEXT    NOT NULL,
  action            TEXT    NOT NULL,
  message           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fires_session ON fires(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_fires_repo    ON fires(repo_key, ts);
"""


@dataclass(frozen=True, slots=True)
class FireRow:
    id: int
    ts: float
    session_id: str
    claude_session_id: str | None
    repo_key: str | None
    hook_name: str
    source_file: str
    event: str
    action: str
    message: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(*(row[f] for f in cls.__dataclass_fields__))


class FireLog:
    """One SQLite row per hook fire, indexed by session for nearest-preceding misfire attribution."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.lock = threading.RLock()

    @classmethod
    def open(cls, path: Path) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        return cls(conn)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        with self.lock, self.conn:
            yield self.conn

    def append(
        self,
        *,
        ts: float,
        session_id: str,
        claude_session_id: str | None,
        repo_key: str | None,
        hook_name: str,
        source_file: str,
        event: str,
        action: str,
        message: str | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO fires(ts, session_id, claude_session_id, repo_key, hook_name, "
                "source_file, event, action, message) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, session_id, claude_session_id, repo_key, hook_name, source_file, event, action, message),
            )

    def fires_for_session(self, session_id: str) -> list[FireRow]:
        with self.lock:
            return [
                FireRow.from_row(r)
                for r in self.conn.execute("SELECT * FROM fires WHERE session_id = ? ORDER BY ts, id", (session_id,))
            ]

    def attribute(
        self,
        session_id: str | None = None,
        *,
        claude_session_id: str | None = None,
        message: str | None = None,
        event: str | None = None,
        near_ts: float | None = None,
    ) -> FireRow | None:
        """Return the single fire whose message is a substring of ``message``, or ``None`` if ambiguous/unfound.

        The session is selected by ``session_id`` (the transcript-path hash the hook runtime recorded) or by
        ``claude_session_id`` (the session UUID carried on transcript events) — the latter is path-independent,
        so it survives the same transcript being reachable under different path spellings. Rows are ordered
        nearest-preceding first (``ts`` then ``id`` descending). The match survives only when every substring
        hit shares one non-empty ``source_file`` — otherwise attribution is ambiguous and we refuse rather
        than risk pointing at the wrong hook.
        """
        match session_id, claude_session_id:
            case None, None:
                raise ValueError("attribute() needs session_id or claude_session_id")
            case _, None:
                clauses, params = ["session_id = ?"], [session_id]
            case _:
                clauses, params = ["claude_session_id = ?"], [claude_session_id]
        if near_ts is not None:
            clauses.append("ts <= ?")
            params.append(near_ts)
        if event is not None:
            clauses.append("event = ?")
            params.append(event)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM fires WHERE {' AND '.join(clauses)} ORDER BY ts DESC, id DESC", params
            ).fetchall()

        matches = [FireRow.from_row(r) for r in rows if message and r["message"] and r["message"] in message]
        match matches:
            case []:
                return None
            case _ if len({m.source_file for m in matches}) > 1:
                return None
            case [winner, *_] if winner.source_file:
                return winner
            case _:
                return None


@cache
def open_fire_log(path: Path) -> FireLog:
    return FireLog.open(path)


def record_fire(entry: RegisteredHook, evt: BaseHookEvent, result: HookResult) -> None:
    """Append one row for a fired hook. The single fire-log write codepath; never raises into dispatch."""
    if os.environ.get("CAPT_HOOK_SPAWNED") or not resolve_fire_log_enabled() or (path := evt.transcript_path) is None:
        return
    open_fire_log(resolve_fire_log_path()).append(
        ts=time.time(),
        session_id=session_hash(path),
        claude_session_id=evt.session_id,
        repo_key=repo_key(),
        hook_name=entry.name,
        source_file=entry.source_file,
        event=evt.event_name.name,
        action=result.action.value,
        message=result.message,
    )
