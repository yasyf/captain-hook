from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import PurePath
from typing import TYPE_CHECKING

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

MAX_WALK = 20
PS_TABLE_ARGV = ("ps", "-A", "-ww", "-o", "pid=,ppid=,pgid=,uid=,lstart=,command=")
PS_TABLE_ENV = {"LC_ALL": "C", "TZ": "UTC"}
PS_TABLE_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(-?\d+)\s+(\w{3} \w{3}\s+\d{1,2} \d\d:\d\d:\d\d \d{4})\s+(.*)$")
PS_ROW_ARGV = ("ps", "-ww", "-o", "pid=,ppid=,pgid=,uid=,lstart=,command=", "-p")
LSOF_CWD_ARGV = ("lsof", "-a", "-d", "cwd", "-Fn", "-p")
PROCESS_CLASSES = {
    "claude": "an agent session",
    "codex": "an agent session",
    "login": "a terminal host",
    "sshd": "a terminal host",
    "tmux": "a terminal multiplexer",
    "screen": "a terminal multiplexer",
    "zellij": "a terminal multiplexer",
    "launchd": "the system service manager",
    "loginwindow": "the login session",
    "WindowServer": "the display server",
    "capt-hookd": "the Captain Hook host",
    "codex-ask": "the codex-ask channel",
    "orca-serve-supervisor": "the Orca serve supervisor",
    "ghostty": "a terminal emulator",
    "kitty": "a terminal emulator",
    "wezterm-gui": "a terminal emulator",
    "Alacritty": "a terminal emulator",
    "iTerm2": "a terminal emulator",
    "Terminal": "a terminal emulator",
}
COMMAND_MARKERS = {
    "daemon-entry.js": "the Orca PTY daemon",
    "Orca Helper": "an Orca helper",
    "Orca.app/": "the Orca app",
    "Captain Hook.app/": "the Captain Hook host",
    "/iTerm.app/": "a terminal emulator",
    "/Terminal.app/": "a terminal emulator",
    "/Ghostty.app/": "a terminal emulator",
    "/kitty.app/": "a terminal emulator",
    "/WezTerm.app/": "a terminal emulator",
    "/Alacritty.app/": "a terminal emulator",
}


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
        while current is not None:
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


@dataclass(frozen=True, slots=True)
class Unreadable:
    reason: str


@dataclass(frozen=True, slots=True)
class Ownership:
    table: ProcessTable
    owner: ProcessRow | None
    protected: frozenset[int]

    @classmethod
    def resolve(cls, table: ProcessTable) -> Ownership:
        start = ov.client_ppid if (ov := reqenv.current()) is not None else os.getpid()
        protected = frozenset(
            pid
            for row in table.rows.values()
            if process_class(row) is not None
            for pid in (row.pid, *(ancestor.pid for ancestor in table.ancestors(row.pid)))
        )
        return cls(table, table.nearest(start, is_agent), protected)


def run_ps(argv: tuple[str, ...], *, timeout: float) -> str | None:
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
    return done.stdout if done.returncode == 0 else None


def parse_row(line: str) -> ProcessRow | None:
    if (match := PS_TABLE_ROW.match(line)) is None:
        return None
    pid, ppid, pgid, uid, started, command = match.groups()
    return ProcessRow(
        int(pid),
        int(ppid),
        int(pgid),
        int(uid),
        datetime.strptime(" ".join(started.split()), "%a %b %d %H:%M:%S %Y"),
        command,
    )


def process_table(*, timeout: float = 2.0) -> ProcessTable | None:
    if (out := run_ps(PS_TABLE_ARGV, timeout=timeout)) is None:
        return None
    rows: dict[int, ProcessRow] = {}
    for line in filter(str.strip, out.splitlines()):
        if (entry := parse_row(line)) is None:
            return None
        rows[entry.pid] = entry
    return ProcessTable(rows)


def usage_row(pid: int, *, timeout: float = 2.0) -> ProcessRow | None:
    if (out := run_ps((*PS_ROW_ARGV, str(pid)), timeout=timeout)) is None:
        return None
    match list(filter(str.strip, out.splitlines())):
        case [line]:
            return parse_row(line)
        case _:
            return None


def process_cwd(pid: int, *, timeout: float = 2.0) -> str | None:
    if sys.platform == "linux":
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    try:
        done = subprocess.run(
            (*LSOF_CWD_ARGV, str(pid)),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return next((line[1:] for line in done.stdout.splitlines() if line.startswith("n")), None)


def children(table: ProcessTable, pid: int) -> tuple[ProcessRow, ...]:
    return tuple(entry for entry in table.rows.values() if entry.ppid == pid and entry.pid != pid)


def process_class(row: ProcessRow) -> str | None:
    if is_claude(row.command.split()):
        return PROCESS_CLASSES["claude"]
    if (label := PROCESS_CLASSES.get(row.argv0)) is not None:
        return label
    return next((label for marker, label in COMMAND_MARKERS.items() if marker in row.command), None)


def is_agent(row: ProcessRow) -> bool:
    return is_claude(row.command.split()) or row.argv0 in {"claude", "codex"}


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
        case [exe, *args] if PurePath(exe).name in {"node", "bun", "deno"}:
            return any(is_claude_cli_js(arg) for arg in args)
        case _:
            return False


def walk_claude_argv(start_pid: int) -> tuple[str, ...]:
    pid = start_pid
    for _ in range(MAX_WALK):
        if (entry := parent_entry(pid)) is None:
            return ()
        ppid, command = entry
        if is_claude(tokens := command.split()):
            return tuple(tokens)
        if ppid <= 1:
            return ()
        pid = ppid
    return ()


@cache
def _cold_claude_argv() -> tuple[str, ...]:
    return walk_claude_argv(os.getpid())


def claude_argv() -> tuple[str, ...]:
    """The whitespace-split command line of the nearest ``claude`` ancestor, ``()`` when none is found.

    Cold, the walk starts at this process and is process-cached. Under a bound request (the
    resident daemon) it walks fresh from the client's parent on every call — a resumed
    session may relaunch with different flags, and per-dispatch memoization already lives on
    the ``BaseHookEvent`` properties that read it.
    """
    if (ov := reqenv.current()) is None:
        return _cold_claude_argv()
    return walk_claude_argv(ov.client_ppid)


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
