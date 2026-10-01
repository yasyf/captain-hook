"""Strict stdlib exec shim for the signed captain-hook client."""

from __future__ import annotations

import os
import sys
from typing import NoReturn

from capt_hook_client.guard_literal import deny_if_mandatory

HOST = (
    os.path.join(os.path.expanduser("~"), "Applications", "Captain Hook.app", "Contents", "Helpers", "capt-hookd")
    if sys.platform == "darwin"
    else os.path.join(os.path.expanduser("~"), ".local", "share", "captain-hook", "host", "capt-hookd")
)


def main() -> NoReturn:
    """Exec the signed Go client for ``run EVENT``; without it, deny the events the session guard prefilters."""
    match sys.argv[1:]:
        case ["run", event] if event and not event.startswith("-"):
            try:
                os.execv(HOST, [HOST, "run", event])
            except OSError as exc:
                _unavailable(event, exc)
        # TODO: delete with the Go `run EVENT --async` alias in 12.32; plugin 12.29 sessions still exec this twin.
        case ["run", event, "--async"] if event and not event.startswith("-"):
            raise SystemExit(0)
        case _:
            _die("usage: hook run EVENT")


def ops_main() -> NoReturn:
    """Exec the fixed Go operations surface without translating arguments."""
    _exec(HOST, [HOST, *sys.argv[1:]])


def _unavailable(event: str, exc: OSError) -> NoReturn:
    if deny_if_mandatory(event, sys.stdin.buffer) == 0:
        raise SystemExit(0)
    _die(f"captain-hook client unavailable at {HOST}: {exc}")


def _exec(path: str, argv: list[str]) -> NoReturn:
    try:
        os.execv(path, argv)
    except OSError as exc:
        _die(f"captain-hook client unavailable at {path}: {exc}", code=1)


def _die(message: str, *, code: int = 1) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)
