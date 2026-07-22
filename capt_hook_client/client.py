"""Strict stdlib exec shim for the fixed signed captain-hook host."""

from __future__ import annotations

import importlib.metadata
import os
import sys
from typing import NoReturn

HOST = "/Applications/Captain Hook.app/Contents/Helpers/capt-hookd"
DIST_NAME = "capt-hook"


def main() -> NoReturn:
    """Translate the one hook-event grammar and exec the fixed Go client."""
    parsed = _parse_run(sys.argv[1:])
    if parsed is None:
        _die("usage: hook [--root ROOT] run EVENT [--async]")
    root, event, async_ = parsed
    cwd = os.getcwd()
    argv = [
        HOST,
        "run",
        "--event",
        event,
        "--root",
        root or os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR") or cwd,
        "--cwd",
        cwd,
        "--python",
        sys.executable,
        "--build",
        importlib.metadata.version(DIST_NAME),
    ]
    if async_:
        argv.append("--async")
    _exec(argv)


def ops_main() -> NoReturn:
    """Exec the fixed Go operations surface without translating arguments."""
    _exec([HOST, *sys.argv[1:]])


def _parse_run(argv: list[str]) -> tuple[str | None, str, bool] | None:
    root: str | None = None
    index = 0
    if len(argv) >= 2 and argv[0] == "--root":
        root, index = argv[1], 2
    elif argv and argv[0].startswith("--root="):
        root, index = argv[0].removeprefix("--root="), 1
    tail = argv[index:]
    if len(tail) not in (2, 3) or tail[0] != "run" or not tail[1] or tail[1].startswith("-"):
        return None
    if len(tail) == 3 and tail[2] != "--async":
        return None
    return root, tail[1], len(tail) == 3


def _exec(argv: list[str]) -> NoReturn:
    try:
        os.execv(HOST, argv)
    except OSError as exc:
        _die(f"captain-hook signed host unavailable at {HOST}: {exc}", code=1)


def _die(message: str, *, code: int = 2) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)
