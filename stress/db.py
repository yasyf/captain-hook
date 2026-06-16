"""Read-only SQLite access for assertions; opens ``mode=ro`` URIs so WAL state is never disturbed."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def query(db: Path, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def one(db: Path, sql: str, params: tuple[object, ...] = ()) -> dict[str, Any] | None:
    return rows[0] if (rows := query(db, sql, params)) else None


def count(db: Path, sql: str, params: tuple[object, ...] = ()) -> int:
    return int(next(iter(row.values()))) if (row := one(db, sql, params)) else 0


def integrity_ok(db: Path) -> bool:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
