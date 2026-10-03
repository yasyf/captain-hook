"""The machine-wide grant store: one SQLite file every session reads, with atomic spends."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from captain_hook.grants.records import Grant, Spend, SpendState
from captain_hook.util.paths import resolve_state_dir

RESERVATION_TTL = timedelta(minutes=2)
SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    tree TEXT NOT NULL,
    source_key TEXT,
    body TEXT NOT NULL,
    UNIQUE (kind, tree, source_key)
);
CREATE TABLE IF NOT EXISTS spends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL REFERENCES grants(id),
    at TEXT NOT NULL,
    state TEXT NOT NULL,
    session TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    relied_on TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS grants_by_kind ON grants (kind, tree);
CREATE INDEX IF NOT EXISTS spends_by_grant ON spends (grant_id);
CREATE INDEX IF NOT EXISTS spends_by_call ON spends (tool_use_id, state);
"""


class SpentError(Exception):
    """A spend found the grant exhausted, expired, or revoked; the message says which and when."""


def grants_path() -> Path:
    return resolve_state_dir() / "hooks" / "grants.db"


def now() -> datetime:
    return datetime.now(UTC)


def stamp(at: datetime) -> str:
    return at.astimezone().strftime("%Y-%m-%d %-I:%M %p")


def new_id() -> str:
    return secrets.token_hex(6)


@contextmanager
def connect() -> Generator[sqlite3.Connection]:
    path = grants_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as db:
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


def parse_spend(row: tuple[Any, ...]) -> Spend:
    sid, grant_id, at, state, session, agent, tool_use_id, fingerprint, summary, reason, relied_on = row
    return Spend(
        id=sid,
        grant_id=grant_id,
        at=datetime.fromisoformat(at),
        state=state,
        session=session,
        agent=agent,
        tool_use_id=tool_use_id,
        fingerprint=fingerprint,
        summary=summary,
        reason=reason,
        relied_on=json.loads(relied_on),
    )


SPEND_COLUMNS = "id, grant_id, at, state, session, agent, tool_use_id, fingerprint, summary, reason, relied_on"


def mint(grant: Grant) -> Grant:
    """Store *grant*, or return the grant already minted from the same approval in the same tree."""
    with connect() as db, immediate(db):
        if grant.source_key is not None and (
            row := db.execute(
                "SELECT body FROM grants WHERE kind = ? AND tree = ? AND source_key = ?",
                (grant.kind, grant.tree, grant.source_key),
            ).fetchone()
        ):
            return Grant.model_validate_json(row[0])
        db.execute(
            "INSERT INTO grants (id, kind, tree, source_key, body) VALUES (?, ?, ?, ?, ?)",
            (grant.id, grant.kind, grant.tree, grant.source_key, grant.model_dump_json()),
        )
        return grant


def save(grant: Grant) -> None:
    with connect() as db:
        db.execute("UPDATE grants SET body = ? WHERE id = ?", (grant.model_dump_json(), grant.id))


def link(grant_id: str, system: str, ident: str) -> Grant:
    """Record *ident* as *grant_id*'s twin in *system*, against the stored record, keeping any revocation."""
    with connect() as db, immediate(db):
        body = db.execute("SELECT body FROM grants WHERE id = ?", (grant_id,)).fetchone()[0]
        current = Grant.model_validate_json(body)
        linked = current.model_copy(update={"links": current.links | {system: ident}})
        db.execute("UPDATE grants SET body = ? WHERE id = ?", (linked.model_dump_json(), grant_id))
        return linked


def load(grant_id: str) -> Grant:
    with connect() as db:
        row = db.execute("SELECT body FROM grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        raise KeyError(f"no grant {grant_id}")
    return Grant.model_validate_json(row[0])


def grants(kind: str | None = None, tree: str | None = None) -> list[Grant]:
    clauses = [(column, value) for column, value in (("kind", kind), ("tree", tree)) if value is not None]
    where = " AND ".join(f"{column} = ?" for column, _ in clauses) or "1"
    with connect() as db:
        rows = db.execute(f"SELECT body FROM grants WHERE {where}", [value for _, value in clauses]).fetchall()
    return sorted((Grant.model_validate_json(row[0]) for row in rows), key=lambda grant: grant.created)


def spends(grant_id: str) -> list[Spend]:
    with connect() as db:
        rows = db.execute(f"SELECT {SPEND_COLUMNS} FROM spends WHERE grant_id = ? ORDER BY id", (grant_id,)).fetchall()
    return [parse_spend(row) for row in rows]


def counted(spend: Spend, at: datetime) -> bool:
    return spend.state == "committed" or (spend.state == "reserved" and at - spend.at < RESERVATION_TTL)


def remaining(grant: Grant, used: list[Spend], at: datetime) -> int | None:
    return None if grant.uses is None else grant.uses - sum(counted(spend, at) for spend in used)


def retried(grant: Grant, used: list[Spend], fingerprint: str | None) -> bool:
    """Whether *fingerprint* repeats the action a one-shot grant was already spent on."""
    return grant.uses == 1 and any(spend.state == "committed" and spend.fingerprint == fingerprint for spend in used)


def unusable(grant: Grant, used: list[Spend], at: datetime, fingerprint: str | None = None) -> str | None:
    """Why *grant* covers nothing at *at*, or ``None`` while it is live or *fingerprint* retries its one use."""
    if grant.revoked is not None:
        return f"grant {grant.id} was revoked at {stamp(grant.revoked)}."
    if grant.expires is not None and grant.expires <= at:
        return f"grant {grant.id} expired at {stamp(grant.expires)}."
    if retried(grant, used, fingerprint):
        return None
    if (left := remaining(grant, used, at)) is not None and left <= 0:
        last = next(spend for spend in reversed(used) if counted(spend, at))
        return f"grant {grant.id} was spent at {stamp(last.at)} by {last.session}/{last.agent} on {last.summary}."
    return None


def matching(kind: str, tree: str, scope: Mapping[str, str], fingerprint: str) -> list[tuple[Grant, str | None]]:
    """The grants of *kind* in *tree* whose scope equals *scope*, newest first, each with why it is unusable."""
    at = now()
    return [
        (grant, unusable(grant, spends(grant.id), at, fingerprint))
        for grant in reversed(grants(kind, tree))
        if grant.scope == dict(scope)
    ]


def reserve(
    grant_id: str,
    *,
    tree: str,
    scope: Mapping[str, str],
    state: SpendState,
    session: str,
    agent: str,
    tool_use_id: str,
    fingerprint: str,
    summary: str,
    reason: str,
    relied_on: list[str],
) -> int | None:
    """Spend one use of *grant_id* atomically and return the uses left; raise :class:`SpentError` when none are.

    A one-shot grant already spent on this exact *fingerprint* covers its retry without a second use,
    so a write whose reply was lost can go again; the downstream system dedupes the effect.
    """
    at = now()
    with connect() as db, immediate(db):
        grant = Grant.model_validate_json(db.execute("SELECT body FROM grants WHERE id = ?", (grant_id,)).fetchone()[0])
        if grant.tree != tree or grant.scope != dict(scope):
            raise SpentError(f"grant {grant.id} covers {grant.scope} in another session tree or destination.")
        rows = db.execute(f"SELECT {SPEND_COLUMNS} FROM spends WHERE grant_id = ? ORDER BY id", (grant_id,)).fetchall()
        used = [parse_spend(row) for row in rows]
        left = remaining(grant, used, at)
        if (why := unusable(grant, used, at, fingerprint)) is not None:
            raise SpentError(why)
        if retried(grant, used, fingerprint):
            return left
        db.execute(
            f"INSERT INTO spends ({SPEND_COLUMNS.removeprefix('id, ')}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id,
                at.isoformat(),
                state,
                session,
                agent,
                tool_use_id,
                fingerprint,
                summary,
                reason,
                json.dumps(relied_on),
            ),
        )
        return None if left is None else left - 1


def settle(tool_use_id: str, *, allowed: bool) -> bool:
    """Commit every use reserved for *tool_use_id* when its event was allowed, else release them.

    A reservation older than :data:`RESERVATION_TTL` stopped holding its use, so it is released and the
    settle reports ``False``: the call it reserved for must not go ahead.
    """
    cutoff = (now() - RESERVATION_TTL).isoformat()
    with connect() as db, immediate(db):
        stale = db.execute(
            "UPDATE spends SET state = 'released' WHERE tool_use_id = ? AND state = 'reserved' AND at <= ?",
            (tool_use_id, cutoff),
        ).rowcount
        db.execute(
            "UPDATE spends SET state = ? WHERE tool_use_id = ? AND state = 'reserved'",
            ("committed" if allowed else "released", tool_use_id),
        )
    return not (allowed and stale)


def revoke(grant_id: str) -> Grant:
    grant = load(grant_id)
    revoked = grant.model_copy(update={"revoked": now()})
    save(revoked)
    return revoked


def expiry(ttl: timedelta | None) -> datetime | None:
    return None if ttl is None else now() + ttl
