from __future__ import annotations

import os
import re
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import PurePath
from typing import TYPE_CHECKING

from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

MAX_WALK = 20
PROBE_TIMEOUT = 5
ANCHOR_CACHE_SIZE = 64
PS_ROW_COLUMNS = "pid=,ppid=,pgid=,uid=,lstart=,command="
PS_TABLE_ARGV = ("ps", "-A", "-ww", "-o", PS_ROW_COLUMNS)
PS_TABLE_ENV = {"LC_ALL": "C", "TZ": "UTC"}
LSTART = r"(\w{3} \w{3}\s+\d{1,2} \d\d:\d\d:\d\d \d{4})"
PS_TABLE_ROW = re.compile(rf"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(-?\d+)\s+{LSTART}\s+(.*)$")
PS_ENVIRONMENT_ROW = re.compile(rf"^\s*{LSTART}\s+(.*)$")


@dataclass(frozen=True, slots=True)
class ProcessRow:
    pid: int
    ppid: int
    pgid: int
    uid: int
    started: datetime
    command: str

    @property
    def argv0(self) -> str:
        return PurePath(head[0]).name if (head := self.command.split(None, 1)) else ""


@dataclass(frozen=True, slots=True)
class ProcessTable:
    rows: Mapping[int, ProcessRow]

    def ancestors(self, pid: int) -> tuple[ProcessRow, ...]:
        chain: list[ProcessRow] = []
        seen = {pid}
        current = self.rows.get(pid)
        while current is not None and len(chain) < MAX_WALK * 4:
            if (parent := self.rows.get(current.ppid)) is None or parent.pid in seen:
                break
            seen.add(parent.pid)
            chain.append(parent)
            current = parent
        return tuple(chain)

    def nearest(self, pid: int, predicate: Callable[[ProcessRow], bool]) -> ProcessRow | None:
        if (row := self.rows.get(pid)) is None:
            return None
        return next((candidate for candidate in (row, *self.ancestors(pid)) if predicate(candidate)), None)

    def descendants(self, pid: int) -> tuple[ProcessRow, ...]:
        children: dict[int, list[ProcessRow]] = {}
        for row in self.rows.values():
            children.setdefault(row.ppid, []).append(row)
        found: list[ProcessRow] = []
        seen = {pid}
        pending = [pid]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child.pid not in seen:
                    seen.add(child.pid)
                    found.append(child)
                    pending.append(child.pid)
        return tuple(found)


class AnchorCache:
    def __init__(self, capacity: int) -> None:
        self._chains: OrderedDict[int, tuple[ProcessRow, ...]] = OrderedDict()
        self._lock = threading.Lock()
        self._capacity = capacity

    def get(self, client_ppid: int) -> tuple[ProcessRow, ...] | None:
        with self._lock:
            if (chain := self._chains.get(client_ppid)) is not None:
                self._chains.move_to_end(client_ppid)
            return chain

    def put(self, client_ppid: int, chain: tuple[ProcessRow, ...]) -> None:
        with self._lock:
            self._chains[client_ppid] = chain
            self._chains.move_to_end(client_ppid)
            while len(self._chains) > self._capacity:
                self._chains.popitem(last=False)

    def discard(self, client_ppid: int) -> None:
        with self._lock:
            self._chains.pop(client_ppid, None)

    def clear(self) -> None:
        with self._lock:
            self._chains.clear()


ANCHORS = AnchorCache(ANCHOR_CACHE_SIZE)


def ps_rows(argv: tuple[str, ...], *, timeout: float) -> dict[int, ProcessRow] | None:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=os.environ | PS_TABLE_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    rows: dict[int, ProcessRow] = {}
    for line in filter(str.strip, done.stdout.splitlines()):
        if (match := PS_TABLE_ROW.match(line)) is None:
            return None
        pid, ppid, pgid, uid, started, command = match.groups()
        rows[int(pid)] = ProcessRow(
            int(pid),
            int(ppid),
            int(pgid),
            int(uid),
            started_at(started),
            command,
        )
    return rows


def process_table(*, timeout: float = 2.0) -> ProcessTable | None:
    return None if (rows := ps_rows(PS_TABLE_ARGV, timeout=timeout)) is None else ProcessTable(rows)


def process_rows(pids: Sequence[int]) -> dict[int, ProcessRow] | None:
    return ps_rows(
        ("ps", "-ww", "-o", PS_ROW_COLUMNS, "-p", ",".join(map(str, pids))),
        timeout=reqenv.clamp_timeout(PROBE_TIMEOUT),
    )


def started_at(lstart: str) -> datetime:
    return datetime.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y")


def environment(row: ProcessRow, *, timeout: float = 2.0) -> str | None:
    """The environment ``ps -E`` prints after *row*'s command, or ``None`` once *row*'s pid runs another process.

    The start time and command must both match *row*, so a pid recycled since the table was read never lends
    *row* another process's environment.
    """
    try:
        done = subprocess.run(
            ("ps", "-E", "-ww", "-o", "lstart=,command=", "-p", str(row.pid)),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=os.environ | PS_TABLE_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or (match := PS_ENVIRONMENT_ROW.match(done.stdout.rstrip("\n"))) is None:
        return None
    started, shown = match.groups()
    if started_at(started) != row.started or not shown.startswith(row.command):
        return None
    return shown.removeprefix(row.command)


def process_start_time(pid: int) -> str | None:
    """The kernel start-time of ``pid`` (``ps -o lstart``), stable across reads for one process.

    Recorded in the daemon meta at startup and re-derived by the ops surface so a recycled pid — a
    crashed daemon's pid reassigned to an unrelated live process — is detected as stale, never
    signalled. Returns ``None`` when the pid is gone or ``ps`` is unavailable.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() or None


def probe(pids: Sequence[int]) -> dict[int, ProcessRow] | None:
    reqenv.checkpoint()
    rows = None if reqenv.deadline_within(0) else process_rows(pids)
    if rows is None and reqenv.deadline_within(0):
        raise EvidenceIncomplete("deadline", "process ancestry walk reached the caller deadline")
    return rows


def next_hop(pid: int) -> ProcessRow | None:
    return None if (rows := probe((pid,))) is None else rows.get(pid)


def is_claude_cli_js(token: str) -> bool:
    return (path := PurePath(token)).name == "cli.js" and any("claude" in part for part in path.parts[:-1])


def is_claude(tokens: list[str]) -> bool:
    match tokens:
        case [exe, *_] if PurePath(exe).name == "claude":
            return True
        case [exe, *args] if PurePath(exe).name in {"node", "bun", "deno"}:
            return any(is_claude_cli_js(arg) for arg in args)
        case _:
            return False


def walk_chain(start_pid: int) -> tuple[ProcessRow, ...] | None:
    if (row := next_hop(start_pid)) is None:
        return None
    chain = [row]
    while not is_claude(row.command.split()):
        if row.ppid <= 1 or len(chain) == MAX_WALK or (row := next_hop(row.ppid)) is None:
            return None
        chain.append(row)
    return tuple(chain)


def chain_argv(chain: tuple[ProcessRow, ...] | None) -> tuple[str, ...]:
    return () if chain is None else tuple(chain[-1].command.split())


def chain_is_live(chain: tuple[ProcessRow, ...]) -> bool:
    rows = probe([row.pid for row in chain])
    return rows is not None and all(rows.get(row.pid) == row for row in chain)


def walk_claude_argv(start_pid: int) -> tuple[str, ...]:
    return chain_argv(walk_chain(start_pid))


def anchored_claude_argv(client_ppid: int) -> tuple[str, ...]:
    if (chain := ANCHORS.get(client_ppid)) is not None:
        if chain_is_live(chain):
            return chain_argv(chain)
        ANCHORS.discard(client_ppid)
    if (chain := walk_chain(client_ppid)) is not None:
        ANCHORS.put(client_ppid, chain)
    return chain_argv(chain)


@cache
def _cold_claude_argv() -> tuple[str, ...]:
    return walk_claude_argv(os.getpid())


def claude_argv() -> tuple[str, ...]:
    """The whitespace-split command line of the nearest ``claude`` ancestor, ``()`` when none is found.

    Cold, the walk starts at this process and is process-cached. Under a bound request (the
    resident daemon) it resolves once per request from the client's parent and every event copy
    and registration of that request shares the result; across requests the walk is reused
    while one probe shows every hop from the client's parent to that ``claude`` unchanged, so
    a resumed session relaunched with different flags is seen on its next request. A walk the
    caller's deadline cuts short raises :class:`EvidenceIncomplete` (``deadline``) rather than
    answering, so a hook reading it fails open or withholds completion as it would for
    transcript evidence.
    """
    if (ov := reqenv.current()) is None:
        return _cold_claude_argv()
    with ov.memo.lock:
        if ov.memo.claude_argv is None:
            ov.memo.claude_argv = anchored_claude_argv(ov.client_ppid)
        return ov.memo.claude_argv


def disallowed_tools(argv: Sequence[str]) -> frozenset[str]:
    names: list[str] = []
    taking = False
    for token in argv:
        if token == "--":
            break
        flag, eq, inline = token.partition("=")
        if flag in {"--disallowedTools", "--disallowed-tools"}:
            names.extend(inline.split(","))
            taking = not eq
        elif token.startswith("-"):
            taking = False
        elif taking:
            names.extend(token.split(","))
    return frozenset(filter(None, names))


def claude_skip_permissions() -> bool:
    """Whether the nearest ``claude`` ancestor launched with a skip-permissions flag."""
    return not {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}.isdisjoint(claude_argv())


def claude_disallowed_tools() -> frozenset[str]:
    """The tool names the nearest ``claude`` ancestor launched with under ``--disallowedTools``."""
    return disallowed_tools(claude_argv())
