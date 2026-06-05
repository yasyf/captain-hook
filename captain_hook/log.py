"""Captain-hook logging: one per-session loguru file sink plus stderr warnings.

Modules log through loguru directly (``from loguru import logger``), attaching
structured context with ``logger.bind(...)``. ``setup_logging()`` installs the
sinks and a patcher that truncates long bound values so call sites never have to
slice them by hand.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.session import session_hash
from captain_hook.settings import resolve_log_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from loguru import Record

MAX_BOUND_VALUE = 200


def truncate_bound_values(record: Record) -> None:
    """Patcher: cap long string values bound via ``logger.bind(...)``."""
    for key, value in record["extra"].items():
        if isinstance(value, str) and len(value) > MAX_BOUND_VALUE:
            record["extra"][key] = value[:MAX_BOUND_VALUE] + "…"


def make_format(template: str) -> Callable[[Record], str]:
    """Build a loguru format function that appends bound context and the traceback."""

    def formatter(record: Record) -> str:
        return (template + " | {extra}" if record["extra"] else template) + "\n{exception}"

    return formatter


def setup_logging(transcript_path: str | None) -> None:
    """Route captain-hook logs to a per-session loguru file sink plus stderr warnings."""
    session_id = session_hash(transcript_path) if transcript_path else "unknown"
    log_dir = resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.configure(
        patcher=truncate_bound_values,
        handlers=[
            {
                "sink": str(log_dir / f"{session_id}.log"),
                "level": "DEBUG",
                "format": make_format("{time:YYYY-MM-DD HH:mm:ss,SSS} {level} {name}: {message}"),
                "encoding": "utf-8",
            },
            {"sink": sys.stderr, "level": "WARNING", "format": make_format("{level}: {message}")},
        ],
    )
