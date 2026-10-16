"""The machine-wide ticket store: one SQLite file every session reads."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from captain_hook.tickets.records import Ticket, Use, render_place
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_state_dir

LOCK_SECONDS = 10.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    tree TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    at TEXT NOT NULL,
    state TEXT NOT NULL,
    session TEXT NOT NULL,
    agent TEXT NOT NULL,
    call TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    summary TEXT NOT NULL,
    UNIQUE (ticket_id, call)
);
CREATE TABLE IF NOT EXISTS orca_terminals (
    handle TEXT NOT NULL,
    tree TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (handle, tree)
);
CREATE INDEX IF NOT EXISTS tickets_by_kind ON tickets (kind, tree);
"""
USE_COLUMNS = "id, ticket_id, at, state, session, agent, call, fingerprint, summary"


@dataclass(frozen=True, slots=True)
class Unusable:
    """Why a ticket covers nothing: plainly for the agent, and with its id and time for the user."""

    agent: str
    detail: str

    def __str__(self) -> str:
        return self.detail


class SpentError(Exception):
    """A spend found the ticket missing, expired, revoked, or covering another action."""

    def __init__(self, why: Unusable) -> None:
        super().__init__(why.detail)
        self.why = why


def tickets_path() -> Path:
    return resolve_state_dir() / "hooks" / "tickets.db"


def now() -> datetime:
    return datetime.now(UTC)


def stamp(at: datetime) -> str:
    return at.astimezone().strftime("%Y-%m-%d %-I:%M %p")


def new_id() -> str:
    return secrets.token_hex(6)


def expiry(ttl: timedelta | None) -> datetime | None:
    return None if ttl is None else now() + ttl


@contextmanager
def connect() -> Generator[sqlite3.Connection]:
    path = tickets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    left = reqenv.seconds_left()
    wait = LOCK_SECONDS if left is None else max(0.0, min(LOCK_SECONDS, left))
    with closing(sqlite3.connect(path, timeout=wait, isolation_level=None)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        yield db


@contextmanager
def immediate(db: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def parse_use(row: tuple[Any, ...]) -> Use:
    uid, ticket_id, at, state, session, agent, call, fingerprint, summary = row
    return Use(
        id=uid,
        ticket_id=ticket_id,
        at=datetime.fromisoformat(at),
        state=state,
        session=session,
        agent=agent,
        call=call,
        fingerprint=fingerprint,
        summary=summary,
    )


def mint(ticket: Ticket) -> Ticket:
    with connect() as db:
        db.execute(
            "INSERT INTO tickets (id, kind, tree, body) VALUES (?, ?, ?, ?)",
            (ticket.id, ticket.kind, ticket.tree, ticket.model_dump_json()),
        )
    return ticket


def save(ticket: Ticket) -> None:
    with connect() as db:
        db.execute("UPDATE tickets SET body = ? WHERE id = ?", (ticket.model_dump_json(), ticket.id))


def load(ticket_id: str) -> Ticket:
    with connect() as db:
        row = db.execute("SELECT body FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise KeyError(f"no ticket {ticket_id}")
    return Ticket.model_validate_json(row[0])


def tickets(kind: str | None = None, trees: Sequence[str] = ()) -> list[Ticket]:
    """The tickets of *kind* minted in any of *trees*, oldest first; every tree when none is named."""
    clauses = [("kind = ?", [kind])] if kind is not None else []
    if trees:
        clauses.append((f"tree IN ({', '.join('?' * len(trees))})", list(trees)))
    where = " AND ".join(clause for clause, _ in clauses) or "1"
    with connect() as db:
        rows = db.execute(
            f"SELECT body FROM tickets WHERE {where}", [value for _, values in clauses for value in values]
        ).fetchall()
    return sorted((Ticket.model_validate_json(row[0]) for row in rows), key=lambda ticket: ticket.created)


def uses(ticket_id: str) -> list[Use]:
    with connect() as db:
        rows = db.execute(f"SELECT {USE_COLUMNS} FROM uses WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
    return [parse_use(row) for row in rows]


def unusable(ticket: Ticket, at: datetime) -> Unusable | None:
    if ticket.revoked is not None:
        return Unusable(
            "The permission that covered this was revoked.",
            f"Ticket {ticket.id} was revoked at {stamp(ticket.revoked)}.",
        )
    if ticket.expires is not None and ticket.expires <= at:
        return Unusable(
            "The permission that covered this has expired.", f"Ticket {ticket.id} expired at {stamp(ticket.expires)}."
        )
    return None


def live(ticket: Ticket, at: datetime | None = None) -> bool:
    return unusable(ticket, at or now()) is None


def covering(kind: str, trees: Sequence[str], place: Mapping[str, str], fingerprint: str) -> Ticket | None:
    """The newest live ticket of *kind* in *trees* that covers the action at *place* with *fingerprint*."""
    at = now()
    return next(
        (
            ticket
            for ticket in reversed(tickets(kind, trees))
            if ticket.covers(place, fingerprint) and unusable(ticket, at) is None
        ),
        None,
    )


def spend(
    ticket_id: str,
    *,
    trees: Sequence[str],
    place: Mapping[str, str],
    fingerprint: str,
    session: str,
    agent: str,
    call: str,
    summary: str,
) -> Ticket:
    """Record that *ticket_id* covered the action *call*; raise :class:`SpentError` when it does not.

    One use is recorded per call, so the hook that found the ticket and the system that spends it
    downstream for the same call record one use between them.
    """
    at = now()
    with connect() as db, immediate(db):
        if (row := db.execute("SELECT body FROM tickets WHERE id = ?", (ticket_id,)).fetchone()) is None:
            raise SpentError(
                Unusable("The permission named for this action does not exist.", f"No ticket {ticket_id} exists.")
            )
        ticket = Ticket.model_validate_json(row[0])
        if ticket.tree not in trees or not ticket.covers(place, fingerprint):
            raise SpentError(
                Unusable(
                    f"The permission named for this action covers {render_place(ticket.place)} in another session.",
                    f"Ticket {ticket.id} covers {render_place(ticket.place)} in session tree {ticket.tree}, which"
                    " does not include this action.",
                )
            )
        if (why := unusable(ticket, at)) is not None:
            raise SpentError(why)
        db.execute(
            f"INSERT OR IGNORE INTO uses ({USE_COLUMNS.removeprefix('id, ')}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, at.isoformat(), "committed", session, agent, call, fingerprint, summary),
        )
    return ticket


def release(ticket_id: str, call: str) -> int:
    """Hand back the use *ticket_id* recorded for *call* when that action never took effect."""
    with connect() as db, immediate(db):
        return db.execute(
            "UPDATE uses SET state = 'released' WHERE ticket_id = ? AND call = ? AND state = 'committed'",
            (ticket_id, call),
        ).rowcount


def revoke(ticket_id: str) -> Ticket:
    revoked = load(ticket_id).model_copy(update={"revoked": now()})
    save(revoked)
    return revoked


def record_terminal(handle: str, tree: str) -> bool:
    """Record that session tree *tree* runs in the Orca terminal *handle*; ``False`` when already recorded."""
    with connect() as db:
        return (
            db.execute(
                "INSERT OR IGNORE INTO orca_terminals (handle, tree, at) VALUES (?, ?, ?)",
                (handle, tree, now().isoformat()),
            ).rowcount
            == 1
        )


def terminal_tree(handle: str) -> str | None:
    """The session tree most recently recorded in the Orca terminal *handle*."""
    with connect() as db:
        row = db.execute(
            "SELECT tree FROM orca_terminals WHERE handle = ? ORDER BY at DESC LIMIT 1", (handle,)
        ).fetchone()
    return None if row is None else row[0]
