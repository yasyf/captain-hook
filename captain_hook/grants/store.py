"""The machine-wide grant store: one SQLite file every session reads, with atomic spends."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from captain_hook.grants.records import Adoption, Grant, Spend, SpendState, covers, render_scope
from captain_hook.util.paths import resolve_state_dir

RESERVATION_TTL = timedelta(minutes=2)
EVIDENCE_KINDS = ("ask", "words")
RECORD_KINDS = (*EVIDENCE_KINDS, "spawn", "shell")
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
CREATE TABLE IF NOT EXISTS adoptions (
    grant_id TEXT NOT NULL REFERENCES grants(id),
    tree TEXT NOT NULL,
    at TEXT NOT NULL,
    session TEXT NOT NULL,
    agent TEXT NOT NULL,
    PRIMARY KEY (grant_id, tree)
);
CREATE TABLE IF NOT EXISTS orca_terminals (
    handle TEXT NOT NULL,
    tree TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (handle, tree)
);
CREATE INDEX IF NOT EXISTS grants_by_kind ON grants (kind, tree);
CREATE INDEX IF NOT EXISTS spends_by_grant ON spends (grant_id);
CREATE INDEX IF NOT EXISTS spends_by_call ON spends (tool_use_id, state);
"""


@dataclass(frozen=True, slots=True)
class Unusable:
    """Why a grant covers nothing: plainly for the agent, and with its id and time for the user."""

    agent: str
    detail: str

    def __str__(self) -> str:
        return self.detail


class SpentError(Exception):
    """A spend found the grant missing, exhausted, expired, or revoked; the message says which and when."""

    def __init__(self, why: Unusable) -> None:
        super().__init__(why.detail)
        self.why = why


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


def mint(grant: Grant, *, across_trees: bool = False) -> Grant:
    """Store *grant*, or return the grant already minted from the same approval in the same tree.

    With *across_trees*, a grant minted from the same approval in any tree is returned instead, so an
    approval recorded outside every session is spent once however many trees reach for it.
    """
    with connect() as db, immediate(db):
        if grant.source_key is not None and (
            row := db.execute(
                "SELECT body FROM grants WHERE kind = ? AND source_key = ? AND (? OR tree = ?)",
                (grant.kind, grant.source_key, across_trees, grant.tree),
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
    """The grants of *kind* usable in *tree*: minted there or adopted into it."""
    clauses = [("kind = ?", [kind])] if kind is not None else []
    if tree is not None:
        clauses.append(("(tree = ? OR id IN (SELECT grant_id FROM adoptions WHERE tree = ?))", [tree, tree]))
    where = " AND ".join(clause for clause, _ in clauses) or "1"
    with connect() as db:
        rows = db.execute(
            f"SELECT body FROM grants WHERE {where}", [value for _, values in clauses for value in values]
        ).fetchall()
    return sorted((Grant.model_validate_json(row[0]) for row in rows), key=lambda grant: grant.created)


def spends(grant_id: str) -> list[Spend]:
    with connect() as db:
        rows = db.execute(f"SELECT {SPEND_COLUMNS} FROM spends WHERE grant_id = ? ORDER BY id", (grant_id,)).fetchall()
    return [parse_spend(row) for row in rows]


def counted(spend: Spend, at: datetime) -> bool:
    return spend.state == "committed" or (spend.state == "reserved" and at - spend.at < RESERVATION_TTL)


def remaining(grant: Grant, used: list[Spend], at: datetime) -> int | None:
    return None if grant.uses is None else grant.uses - sum(counted(spend, at) for spend in used)


def retried(grant: Grant, used: list[Spend], fingerprint: str | None, at: datetime, replay: timedelta) -> bool:
    """Whether *fingerprint* repeats, within *replay* of it, the action a one-shot grant was spent on."""
    return grant.uses == 1 and any(
        spend.state == "committed" and spend.fingerprint == fingerprint and at - spend.at < replay for spend in used
    )


def unusable(
    grant: Grant, used: list[Spend], at: datetime, fingerprint: str | None = None, replay: timedelta = timedelta.max
) -> Unusable | None:
    """Why *grant* covers nothing at *at*, or ``None`` while it is live or *fingerprint* retries its one use."""
    if grant.revoked is not None:
        return Unusable(
            "The approval that covered this was revoked.", f"Grant {grant.id} was revoked at {stamp(grant.revoked)}."
        )
    if grant.expires is not None and grant.expires <= at:
        return Unusable(
            "The approval that covered this has expired.", f"Grant {grant.id} expired at {stamp(grant.expires)}."
        )
    if retried(grant, used, fingerprint, at, replay):
        return None
    if (left := remaining(grant, used, at)) is not None and left <= 0:
        last = next(spend for spend in reversed(used) if counted(spend, at))
        return Unusable(
            f"The approval that covered this was already used on {last.summary}.",
            f"Grant {grant.id} was spent at {stamp(last.at)} by {last.session}/{last.agent} on {last.summary}.",
        )
    return None


def matching(
    kind: str, tree: str, scope: Mapping[str, str], fingerprint: str, replay: timedelta = timedelta.max
) -> list[tuple[Grant, Unusable | None]]:
    """The grants of *kind* in *tree* whose scope covers *scope*, newest first, each with why it is unusable."""
    at = now()
    return [
        (grant, unusable(grant, spends(grant.id), at, fingerprint, replay))
        for grant in reversed(grants(kind, tree))
        if covers(grant.scope, scope)
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
    replay: timedelta = timedelta.max,
) -> int | None:
    """Spend one use of *grant_id* atomically and return the uses left; raise :class:`SpentError` when none are.

    A one-shot grant already spent on this exact *fingerprint*, less than *replay* ago, covers its retry
    without a second use, so a write whose reply was lost can go again; the downstream system dedupes
    the effect.
    """
    at = now()
    with connect() as db, immediate(db):
        if (row := db.execute("SELECT body FROM grants WHERE id = ?", (grant_id,)).fetchone()) is None:
            raise SpentError(
                Unusable("The permission named for this action does not exist.", f"No grant {grant_id} exists.")
            )
        grant = Grant.model_validate_json(row[0])
        adopted = db.execute("SELECT 1 FROM adoptions WHERE grant_id = ? AND tree = ?", (grant_id, tree)).fetchone()
        if (grant.tree != tree and adopted is None) or not covers(grant.scope, scope):
            raise SpentError(
                Unusable(
                    f"The permission named for this action covers {render_scope(grant.scope)} in another session.",
                    f"Grant {grant.id} covers {render_scope(grant.scope)} in session tree {grant.tree}, which does not"
                    " include this action.",
                )
            )
        rows = db.execute(f"SELECT {SPEND_COLUMNS} FROM spends WHERE grant_id = ? ORDER BY id", (grant_id,)).fetchall()
        used = [parse_spend(row) for row in rows]
        left = remaining(grant, used, at)
        if (why := unusable(grant, used, at, fingerprint, replay)) is not None:
            raise SpentError(why)
        if retried(grant, used, fingerprint, at, replay):
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


def release(grant_id: str, tool_use_id: str) -> int:
    """Hand back the use *grant_id* committed for *tool_use_id* when that action never took effect.

    Returns how many uses came back: zero when the call spent nothing, such as a retry of a spent one-shot.
    """
    with connect() as db, immediate(db):
        return db.execute(
            "UPDATE spends SET state = 'released' WHERE grant_id = ? AND tool_use_id = ? AND state = 'committed'",
            (grant_id, tool_use_id),
        ).rowcount


def approval_spends(key: str) -> list[Spend]:
    """Committed uses of every grant minted from the approval *key*, oldest first."""
    with connect() as db:
        rows = db.execute(
            f"SELECT {', '.join(f's.{column}' for column in SPEND_COLUMNS.split(', '))} FROM spends s"
            " WHERE s.state = 'committed' AND s.grant_id IN (SELECT g.id FROM grants g,"
            " json_each(g.body, '$.evidence') e WHERE json_extract(e.value, '$.key') = ?) ORDER BY s.id",
            (key,),
        ).fetchall()
    return [parse_spend(row) for row in rows]


def adopt(grant_id: str, *, tree: str, session: str, agent: str) -> Adoption:
    """Make *grant_id* usable in *tree* too, sharing its budget, and log who adopted it."""
    load(grant_id)
    adoption = Adoption(grant_id=grant_id, tree=tree, at=now(), session=session, agent=agent)
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO adoptions (grant_id, tree, at, session, agent) VALUES (?, ?, ?, ?, ?)",
            (grant_id, tree, adoption.at.isoformat(), session, agent),
        )
    return adoption


def adopt_tree(source: str, *, tree: str, session: str, agent: str) -> int:
    """Adopt every spendable grant minted in *source* into *tree*, logged like :func:`adopt`.

    The owner's recorded words and answers, and the spawns and shells recorded for each agent, stay in their
    own tree, so a second tree never mints a fresh budget from an approval *source* already spent. Returns how
    many adoptions were new.
    """
    with connect() as db:
        return db.execute(
            "INSERT OR IGNORE INTO adoptions (grant_id, tree, at, session, agent)"
            " SELECT id, ?, ?, ?, ? FROM grants WHERE tree = ? AND kind NOT IN (SELECT value FROM json_each(?))",
            (tree, now().isoformat(), session, agent, source, json.dumps(RECORD_KINDS)),
        ).rowcount


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


def adoptions(grant_id: str) -> list[Adoption]:
    with connect() as db:
        rows = db.execute(
            "SELECT grant_id, tree, at, session, agent FROM adoptions WHERE grant_id = ? ORDER BY at", (grant_id,)
        ).fetchall()
    return [
        Adoption(grant_id=row[0], tree=row[1], at=datetime.fromisoformat(row[2]), session=row[3], agent=row[4])
        for row in rows
    ]


def revoke(grant_id: str) -> Grant:
    grant = load(grant_id)
    revoked = grant.model_copy(update={"revoked": now()})
    save(revoked)
    return revoked


def expiry(ttl: timedelta | None) -> datetime | None:
    return None if ttl is None else now() + ttl
