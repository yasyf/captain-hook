"""Strict stdlib exec shim for the plain captain-hook client."""

from __future__ import annotations

import os
import sys
from typing import NoReturn

HOST = os.path.join(
    os.path.expanduser("~"),
    "Applications",
    "Captain Hook.app",
    "Contents",
    "Helpers",
    "capt-hookd",
)
CLIENT = os.path.join(os.path.expanduser("~"), ".daemonkit", "bin", "capt-hookd")


def main() -> NoReturn:
    """Translate the one hook-event grammar and exec the plain Go client."""
    parsed = _parse_run(sys.argv[1:])
    if parsed is None:
        _die("usage: hook [--root ROOT] run EVENT [--async]")
    root, event, async_ = parsed
    if async_:
        raise SystemExit(0)
    if root:
        os.environ["CLAUDE_PROJECT_DIR"] = root
    _exec(CLIENT, [CLIENT, "hook", event])


def ops_main() -> NoReturn:
    """Exec the fixed Go operations surface without translating arguments."""
    _exec(HOST, [HOST, *sys.argv[1:]])


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


def _exec(path: str, argv: list[str]) -> NoReturn:
    try:
        os.execv(path, argv)
    except OSError as exc:
        _die(f"captain-hook client unavailable at {path}: {exc}", code=1)


def _die(message: str, *, code: int = 1) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)
