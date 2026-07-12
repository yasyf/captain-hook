from __future__ import annotations

import os
import subprocess
from functools import cache
from pathlib import PurePath

from captain_hook.util import reqenv
from captain_hook.util.caching import LRUDict

SKIP_PERMISSIONS_FLAGS = frozenset({"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"})
JS_RUNTIMES = frozenset({"node", "bun", "deno"})
MAX_WALK = 20

_SKIP_CACHE: LRUDict[str, bool] = LRUDict(512)


def parent_entry(pid: int) -> tuple[int, str] | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match out.split(None, 1):
        case [ppid, command]:
            return int(ppid), command.strip()
        case [ppid]:
            return int(ppid), ""
        case _:
            return None


def is_claude_cli_js(token: str) -> bool:
    return (path := PurePath(token)).name == "cli.js" and any("claude" in part for part in path.parts[:-1])


def is_claude(tokens: list[str]) -> bool:
    match tokens:
        case [exe, *_] if PurePath(exe).name == "claude":
            return True
        case [exe, *args] if PurePath(exe).name in JS_RUNTIMES:
            return any(is_claude_cli_js(arg) for arg in args)
        case _:
            return False


def walk_skip_permissions(start_pid: int) -> bool:
    pid = start_pid
    for _ in range(MAX_WALK):
        if (entry := parent_entry(pid)) is None:
            return False
        ppid, command = entry
        if is_claude(tokens := command.split()):
            return not SKIP_PERMISSIONS_FLAGS.isdisjoint(tokens)
        if ppid <= 1:
            return False
        pid = ppid
    return False


@cache
def _cold_skip_permissions() -> bool:
    return walk_skip_permissions(os.getpid())


def claude_skip_permissions() -> bool:
    """Whether the nearest ``claude`` ancestor launched with a skip-permissions flag.

    Cold, the walk starts at this process and is process-cached. Under a bound request (the
    resident daemon) it walks from the client's parent and caches per session id, so one
    worker never leaks one session's launch flags into another's.
    """
    if (ov := reqenv.current()) is None:
        return _cold_skip_permissions()
    if ov.session_id not in _SKIP_CACHE:
        _SKIP_CACHE[ov.session_id] = walk_skip_permissions(ov.client_ppid)
    return _SKIP_CACHE[ov.session_id]
