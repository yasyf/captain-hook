from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial, reduce
from itertools import product
from math import prod
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, ClassVar

from cc_transcript.tools import BashCall
from loguru import logger
from pydantic import BaseModel

from captain_hook import Event, Input, LambdaCondition, WorkflowState, on, workflow_state
from captain_hook.bindings import Resolution, Resolved, Unknown, Unresolved, program_name, references
from captain_hook.cmd import Cmd
from captain_hook.command_schemas import ORCA, OSASCRIPT
from captain_hook.grants import (
    Allowed,
    Asked,
    Denied,
    Evidence,
    Grants,
    Judge,
    OwnerWords,
    Proposal,
    Rulings,
    StandingRulings,
)
from captain_hook.grants.evidence import EvidenceSource, children, names
from captain_hook.guard_literal import FOLD_TABLE, GUARDED_WORD, QUOTING_CHARS, names_guarded
from captain_hook.util import proc, reqenv
from captain_hook.util.payload import command_texts
from captain_hook.util.shell import safe_parse_command_line

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from cc_transcript.command import Word

    from captain_hook import BaseHookEvent, HookResult, ToolRewriteEvent
    from captain_hook.cmd import Call
    from captain_hook.command_schema import Arguments, Scalar
    from captain_hook.util.proc import ProcessRow, ProcessTable

GUARDED_PROGRAMS = (
    "kill",
    "pkill",
    "killall",
    "killall5",
    "fuser",
    "skill",
    "snice",
    "kill-port",
    "orca",
    "launchctl",
    "osascript",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "renice",
    "tmux",
    "softwareupdate",
    "pmset",
)
LAUNCHERS = (
    "caffeinate",
    "setsid",
    "builtin",
    "noglob",
    "stdbuf",
    "unbuffer",
    "script",
    "watch",
    "parallel",
    "chrt",
    "ionice",
    "taskset",
    "arch",
    "su",
    "chroot",
)
GUARDED_PROGRAM = re.compile(rf"\b({'|'.join(GUARDED_PROGRAMS)})\b", re.IGNORECASE)
QUOTED_WORD = re.compile(r'"(?:[^"\\]|\\.)*"')
ARG_TOKEN_BREAK = re.compile(r"[\s;|&()<>=`]+")
TEST_BUILTINS = frozenset({"[", "[["})
UNQUOTABLE = re.compile(r"""[\s'"\\$`;&|<>()#]""")
VARIANT_LIMIT = 64
NEGATIVE_TARGET = re.compile(r"-\d+")
DO_SHELL_SCRIPT = re.compile(r'(?i)\bdo\s+shell\s+script\b\s*(?:"((?:[^"\\]|\\.)*)")?')
APPLESCRIPT_ESCAPE = re.compile(r"\\(.)")
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
AGENT_SHIM_PREFIXES = ("cc-", "orca-")
VERIFY = "Verify a pid you started with `ps -o pid,ppid,pgid,lstart,command -p <pid>`"
KILL_FIX = f"{VERIFY} and run `kill <pid>` alone."
RENICE_FIX = f"{VERIFY} and run `renice -n <priority> -p <pid>` alone."
SPELLING_LIMIT = 60
SESSION_ENV = re.compile(r"(?<!\S)CLAUDE_CODE_SESSION_ID=(\S*)")
PROBE_TIMEOUT = 2.0
PROBE_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"))
SCAN_CACHE_LIMIT = 16
IDLE_WAIT_MS = 1000
SCREEN_LINES = 40
PROMPT_WINDOW = 8
WORKER_PAGE = 100
BORDER = re.compile(r"^[─━\s]*$")
BUSY = ("esc to interrupt", "ctrl+c to interrupt", "Running…", "background terminal running")
CLAUDE_PROMPT = "❯"
CODEX_PROMPT = "› Ask Codex to do anything"
DONE_MARKER = re.compile(r"^✻ .* · done ")
LANE_NAME = re.compile(r"[\w.-]+")
LAUNCH_RECEIPTS = ".claude/scratch/orca-launch"
RECEIPT_SLACK = timedelta(seconds=1)
RETRY_WINDOW = timedelta(minutes=2)
BACKGROUNDED = re.compile(r"(?<![&>|])&(?![&>])|\b(?:nohup|setsid|disown)\b")
SCRATCH_PREFIX = "_scratch-"
RECORDED_SINCE = datetime(2026, 10, 4, 2)
LATER_SESSION = "name it in a cc-notes answer for a later session"
SETTLED = frozenset({"completed", "failed"})
CLASS_RULINGS = ("c9b27c1", "6190a4a")
SETTLED_CLOSE_RULES = (
    "The owner's standing rulings in the evidence cover a class of Orca terminal closes rather than one named "
    "terminal: c9b27c1 lets the root close a settled dispatch's idle terminal, as orca-gc does, and 6190a4a lets it "
    "close an orphan terminal its own failed launch created. The proposed action's payload is Orca's record for the "
    "terminal: the dispatch holding it, its Run, and that dispatch's status. The guard has already proven the status "
    "is completed or failed, the terminal's agent idles at a prompt, and the caller is the root session coordinating "
    "that Run. Allow only when a ruling's words cover closing "
    "a terminal with this record, and cite that ruling. Deny when the rulings exclude this record, or limit the class "
    "to terminals it is not."
)
OWNER_NAMED_RULES = (
    "The pending action ends something that may host a session: it closes an Orca terminal, stops, restarts, or "
    "unloads a launchd service, or stops a background task with TaskStop. The scope names the exact terminal handle, "
    "service label, or task id. Allow only when the owner's own words, a prompt, a message they queued, or an "
    "AskUserQuestion answer, name this exact target, by its handle, label, or id, or by the lane or agent name a "
    "`<name>@session-<id>` task id begins with, and ask for it to be closed, stopped, restarted, or kicked, or pick an "
    "option that does. Words about another target, a general instruction to clean up, and words that only describe "
    "the target permit nothing. One owner instruction permits one such action."
)
OWNER_NAMED_TTL = timedelta(hours=1)
TEAMMATE_TASK = re.compile(r"[\w.-]+@session-[0-9a-f]{8}")
SPAWN_TOOLS = frozenset({"Agent", "Task"})
INLINE_LOGIN = "/usr/bin/login -flpq dev /bin/bash --noprofile --norc -p -c orca-tcc-login"
INLINE_TABLE = (
    "    1     0     1    0 Thu Jan  1 00:00:00 2026 /sbin/launchd\n"
    "  900     1   900  501 Thu Jan  1 00:00:00 2026 /Applications/Captain Hook.app/Contents/Helpers/capt-hookd serve\n"
    " 1445     1  1445  501 Thu Jan  1 00:00:00 2026 /Applications/Orca.app/Contents/MacOS/Orca\n"
    " 1743  1445  1743  501 Thu Jan  1 00:00:00 2026 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app/"
    "Contents/MacOS/Orca Helper /Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js\n"
    f"14545  1743 14545    0 Thu Jan  1 00:00:00 2026 {INLINE_LOGIN} /bin/zsh\n"
    "14550 14545 14550  501 Thu Jan  1 00:00:00 2026 -/bin/zsh -l\n"
    "14575 14550 14575  501 Thu Jan  1 00:00:00 2026 claude --dangerously-skip-permissions\n"
    "27103 14575 27103  501 Thu Jan  1 00:00:00 2026 /bin/zsh -c capt-hook run PreToolUse\n"
    "31337 14575 31337  501 Thu Jan  1 00:00:00 2026 sleep 60\n"
    f"15000  1743 15000    0 Thu Jan  1 00:00:00 2026 {INLINE_LOGIN} /opt/homebrew/bin/fish\n"
    "15001 15000 15001  501 Thu Jan  1 00:00:00 2026 -/opt/homebrew/bin/fish -l\n"
    f"16000  1743 16000    0 Thu Jan  1 00:00:00 2026 {INLINE_LOGIN} /opt/homebrew/bin/fish\n"
    "16001 16000 16001  501 Thu Jan  1 00:00:00 2026 -/opt/homebrew/bin/fish -l\n"
    "16002 16001 16002  501 Thu Jan  1 00:00:00 2026 claude --dangerously-skip-permissions --effort xhigh\n"
    f"17000  1743 17000    0 Thu Jan  1 00:00:00 2026 {INLINE_LOGIN} /opt/homebrew/bin/fish\n"
    "17001 17000 17001  501 Thu Jan  1 00:00:00 2026 -/opt/homebrew/bin/fish -l\n"
    "17002 17001 17002  501 Thu Jan  1 00:00:00 2026 /Users/dev/.daemonkit/cache/ab/cc-slack watch --channel C1\n"
)
INLINE_TERMINALS = {"term_idle": 15000, "term_agent": 16000, "term_shim": 17000, "term_gone": 18000}
INLINE_SESSION = "c0ffee00-0000-4000-8000-000000000000"
INLINE_OWNER_TERMINAL = "term_c59a87bf-0000-4000-8000-000000000000"
INLINE_TRANSCRIPT = [{"type": "user", "message": {"role": "user", "content": "Ship the release."}}]
INLINE_STARTED = datetime(2026, 1, 1, tzinfo=UTC)


def inline_ruling(body: str, *, written: datetime = INLINE_STARTED - timedelta(days=1)) -> str:
    return json.dumps(
        [{"id": "0207568aaaa", "title": "Close terminals", "body": body, "updated_at": written.isoformat()}]
    )


def inline_create(
    handle: str, command: str = "orca terminal create --worktree active --command codex --json"
) -> list[dict[str, Any]]:
    return [
        *INLINE_TRANSCRIPT,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_create", "name": "Bash", "input": {"command": command}}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_create",
                        "content": json.dumps({"ok": True, "result": {"terminal": {"handle": handle}}}),
                    }
                ],
            },
        },
    ]


def inline_spawn(task: str, *, tool: str = "Agent", status: str = "teammate_spawned") -> list[dict[str, Any]]:
    name, _, team = task.partition("@")
    return [
        *INLINE_TRANSCRIPT,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_spawn", "name": tool, "input": {"name": name}}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_spawn", "content": f"agent_id: {task}"}],
            },
            "toolUseResult": {"status": status, "teammate_id": task, "agent_id": task, "name": name, "team_name": team},
        },
    ]


def inline_workers(*handles: str) -> str:
    rows = [{"dispatchId": f"ctx_{index}", "agentTerminalHandle": handle} for index, handle in enumerate(handles)]
    return json.dumps({"ok": True, "result": {"workers": rows, "page": {}, "scope": {"source": "all"}}})


def inline_worker(handle: str, status: str, stage: str = "settled") -> str:
    row = {
        "dispatchId": "ctx_settled",
        "runId": "run_inline",
        "dispatchStatus": status,
        "agentTerminalHandle": handle,
        "projection": {"stage": {"detail": stage}},
    }
    return json.dumps({"ok": True, "result": {"workers": [row], "page": {}, "scope": {"source": "all"}}})


def inline_class_rulings(*, written: datetime = INLINE_STARTED - timedelta(days=1)) -> dict[str, str]:
    bodies = {
        "c9b27c1": "Owner: after a dispatch settles the root runs orca-gc to close that dispatch's idle terminal; "
        "never a live or unsettled dispatch.",
        "6190a4a": "Owner: the root may close an orphan Orca terminal its own failed launch created.",
    }
    return {
        f"ccn answer show {ident}": json.dumps(
            {"id": f"{ident}aaaa", "title": "Close settled terminals", "body": body, "updated_at": written.isoformat()}
        )
        for ident, body in bodies.items()
    }


def inline_run(coordinator: str) -> dict[str, str]:
    shown = {"ok": True, "result": {"run": {"id": "run_inline", "coordinator_handle": coordinator}}}
    return {"orca orchestration run-show --id run_inline": json.dumps(shown)}


def inline_screen(*tail: str) -> str:
    return json.dumps({"ok": True, "result": {"terminal": {"source": "screen", "tail": list(tail)}}})


def inline_tab(handle: str, *panes: str) -> dict[str, str]:
    leaves = [{"type": "terminal", "handle": pane, "tabId": "tab-1"} for pane in panes]
    layout = leaves[0] if len(leaves) == 1 else {"type": "split", "children": leaves}
    shown = {"ptyId": f"pty-{handle}", "tabId": "tab-1", "worktreePath": "/w"}
    return {
        f"orca terminal show --terminal {handle} --json": json.dumps({"result": {"terminal": shown}}),
        "orca terminal list --worktree path:/w": json.dumps(
            {"result": {"visualLayouts": [{"root": {"tabs": [{"tabId": "tab-1", "panes": layout}]}}]}}
        ),
    }


INLINE_IDLE = inline_screen("✻ Worked for 2m · done ", "", "─" * 20, CLAUDE_PROMPT, "─" * 20)
INLINE_BUSY = inline_screen("✶ Thinking… (esc to interrupt)", "─" * 20, CLAUDE_PROMPT, "─" * 20)
INLINE_COMMANDS = {
    "ps -A -ww -o": INLINE_TABLE,
    "ps -E -ww -o lstart=,command= -p": "",
    "ps -E -ww -o lstart=,command= -p 31337": (
        f"Thu Jan  1 00:00:00 2026 sleep 60 CLAUDE_CODE_SESSION_ID={INLINE_SESSION}\n"
    ),
    "orca terminal show": "{}",
    **{
        f"orca terminal show --terminal {handle} --json": json.dumps(
            {"result": {"terminal": {"ptyId": f"pty-{handle}"}}}
        )
        for handle in INLINE_TERMINALS
    },
    "orca diagnostics memory --json": json.dumps(
        {
            "result": {
                "worktrees": [
                    {
                        "sessions": [
                            {"sessionId": f"pty-{handle}", "pid": pid} for handle, pid in INLINE_TERMINALS.items()
                        ]
                    }
                ]
            }
        }
    ),
    "ccn answer search": "[]",
    f"ccn answer search {INLINE_OWNER_TERMINAL}": inline_ruling(f"Owner: close exactly {INLINE_OWNER_TERMINAL}."),
    "orca orchestration worker-list": inline_workers(),
    "orca terminal wait": json.dumps({"ok": True, "result": {"wait": {"satisfied": True}}}),
    "orca terminal read": INLINE_IDLE,
}

guarded = partial(Input, commands=INLINE_COMMANDS, transcript=INLINE_TRANSCRIPT)


def nested(depth: int, payload: str, *, wrapper: str = "bash -c") -> str:
    return reduce(lambda acc, _: f"{wrapper} {shlex.quote(acc)}", range(depth), payload)


def clip(text: str, limit: int = SPELLING_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def spell(call: Call) -> str:
    return clip(call.source.raw)


def double_quoted(text: str) -> bool:
    return QUOTED_WORD.fullmatch(text) is not None


def process_class(row: ProcessRow) -> str | None:
    if proc.is_claude(row.command.split()):
        return PROCESS_CLASSES["claude"]
    if (label := PROCESS_CLASSES.get(row.argv0)) is not None:
        return label
    return next((label for marker, label in COMMAND_MARKERS.items() if marker in row.command), None)


def is_agent(row: ProcessRow) -> bool:
    return proc.is_claude(row.command.split()) or row.argv0 in {"claude", "codex"}


def hosts_agent(row: ProcessRow) -> bool:
    return is_agent(row) or process_class(row) is not None or row.argv0.startswith(AGENT_SHIM_PREFIXES)


def describe(row: ProcessRow) -> str:
    return f"pid {row.pid} (`{clip(row.command, 40)}`)"


@dataclass(frozen=True, slots=True)
class Unreadable:
    reason: str


@dataclass(frozen=True, slots=True)
class TimedOut(Unreadable):
    seconds: float


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


def probe_timeout(reason: str, ceiling: float = PROBE_TIMEOUT) -> float | Unreadable:
    if (budget := reqenv.seconds_left()) is None:
        return ceiling
    if budget < 0.5:
        return Unreadable(f"the caller deadline is too close to {reason}")
    return min(ceiling, budget - 0.25)


def probe(argv: tuple[str, ...], ceiling: float = PROBE_TIMEOUT, *, unset: tuple[str, ...] = ()) -> str | Unreadable:
    if isinstance(timeout := probe_timeout(f"run `{argv[0]}`", ceiling), Unreadable):
        return timeout
    env = {key: value for key, value in os.environ.items() if key not in unset}
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env | {"PATH": os.pathsep.join((os.environ["PATH"], *PROBE_FALLBACK_DIRS))},
        )
    except FileNotFoundError:
        return Unreadable(f"`{argv[0]}` is not installed on the hook's PATH")
    except subprocess.TimeoutExpired:
        return TimedOut(f"`{argv[0]}` timed out after {timeout:g}s", timeout)
    except (OSError, subprocess.SubprocessError):
        return Unreadable(f"`{argv[0]}` could not run")
    return done.stdout if done.returncode == 0 else Unreadable(f"`{argv[0]}` failed")


def terminal_pid(handle: str) -> int | Unreadable:
    shown = probe(("orca", "terminal", "show", "--terminal", handle, "--json"))
    if isinstance(shown, Unreadable):
        return shown
    sweep = probe(("orca", "diagnostics", "memory", "--json"))
    if isinstance(sweep, Unreadable):
        return sweep
    try:
        pty = json.loads(shown)["result"]["terminal"]["ptyId"]
        pids = {
            session["sessionId"]: int(session["pid"])
            for worktree in json.loads(sweep)["result"]["worktrees"]
            for session in worktree.get("sessions", ())
        }
        return pids[pty]
    except (ValueError, TypeError, KeyError):
        return Unreadable("Orca reports no process for the terminal")


class SpawnRecord(BaseModel):
    pid: int
    pgid: int
    started: datetime
    command: str
    agent: str

    @classmethod
    def of(cls, row: ProcessRow, agent: str) -> SpawnRecord:
        return cls(pid=row.pid, pgid=row.pgid, started=row.started, command=row.command, agent=agent)

    @property
    def key(self) -> str:
        return f"{self.pid}@{self.started.isoformat()}"

    def matches(self, row: ProcessRow) -> bool:
        return (self.pid, self.started, self.command) == (row.pid, row.started, row.command)


@workflow_state("spawned_processes")
class Spawns(WorkflowState):
    pending: dict[str, datetime] = {}
    records: dict[str, SpawnRecord] = {}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def backgrounds(evt: BaseHookEvent) -> bool:
    return isinstance(call := evt.input, BashCall) and (
        bool(call.run_in_background) or BACKGROUNDED.search(call.command) is not None
    )


def launched(row: ProcessRow, names: frozenset[str]) -> bool:
    return any(PurePath(word).name in names for word in row.command.split()[:2])


def foreign(row: ProcessRow, session: str) -> bool:
    if isinstance(timeout := probe_timeout("read a process environment"), Unreadable):
        return False
    ids = set(SESSION_ENV.findall(proc.environment(row, timeout=timeout) or ""))
    return bool(ids) and ids != {session}


def spawned_rows(ownership: Ownership, since: datetime, names: frozenset[str], session: str) -> list[ProcessRow]:
    table = ownership.table
    floor = since - RECEIPT_SLACK
    groups: dict[int, list[ProcessRow]] = {}
    for row in table.rows.values():
        groups.setdefault(row.pgid, []).append(row)
    fresh = {
        pgid for pgid, members in groups.items() if pgid not in table.rows and all(m.started >= floor for m in members)
    }
    agents = {row.pid: table.nearest(row.ppid, is_agent) for row in table.rows.values() if row.started >= floor}
    rows = [
        row
        for row in table.rows.values()
        if row.pid in agents
        and row.pid not in ownership.protected
        and not hosts_agent(row)
        and (
            (agents[row.pid] is None and row.pgid in fresh)
            or (ownership.owner is not None and agents[row.pid] == ownership.owner)
        )
        and launched(row, names)
        and not foreign(row, session)
    ]
    orphans = {row.pgid for row in rows if agents[row.pid] is None}
    return rows if len(orphans) <= 1 else [row for row in rows if agents[row.pid] is not None]


def scratch_orphan(row: ProcessRow) -> bool:
    worktrees = Path.home() / ".claude" / "worktrees"
    return row.started < RECORDED_SINCE and any(
        PurePath(word).is_relative_to(worktrees)
        and len(parts := PurePath(word).relative_to(worktrees).parts) > 2
        and parts[1].startswith(SCRATCH_PREFIX)
        for word in row.command.split()[:2]
    )


class Facts:
    def __init__(self, session: str | None = None, spawns: Iterable[SpawnRecord] = ()) -> None:
        self.session = session
        self.spawns = tuple(spawns)
        self._guard = threading.Lock()
        self._ownership: Ownership | Unreadable | None = None

    def started_here(self, row: ProcessRow) -> bool:
        if self.session is None or isinstance(timeout := probe_timeout("read a process environment"), Unreadable):
            return False
        shown = proc.environment(row, timeout=timeout)
        return shown is not None and set(SESSION_ENV.findall(shown)) == {self.session}

    def spawned_here(self, row: ProcessRow) -> bool:
        return any(record.matches(row) for record in self.spawns)

    @property
    def ownership(self) -> Ownership | Unreadable:
        with self._guard:
            if self._ownership is None:
                self._ownership = read_ownership()
            return self._ownership

    def terminal_tree(self, handle: str) -> tuple[ProcessRow, ...] | Unreadable:
        if isinstance(ownership := self.ownership, Unreadable):
            return ownership
        if isinstance(pid := terminal_pid(handle), Unreadable):
            return pid
        if (root := ownership.table.rows.get(pid)) is None:
            return Unreadable(f"the terminal's process {pid} is not in the process table")
        return (root, *ownership.table.descendants(pid))


def read_ownership() -> Ownership | Unreadable:
    if isinstance(timeout := probe_timeout("read the process table"), Unreadable):
        return timeout
    table = proc.process_table(timeout=timeout)
    return Unreadable("the process table could not be read") if table is None else Ownership.resolve(table)


def unresolvable(spelling: str, reason: str, fix: str) -> str:
    return f"BLOCKED: `{spelling}` cannot be verified: {reason}. {fix}"


def hidden_behind(arguments: Arguments) -> str:
    if arguments.unread:
        return f"`{clip(arguments.unread[0].raw, 40)}`, an option the guard cannot read"
    return "a command substitution"


def literal_pid(word: Word, value: Scalar | None) -> int | None:
    match value:
        case str() if value.isascii() and value.isdecimal() and value == str(int(value)) and not word.expandable:
            return int(value) or None
        case _:
            return None


def describe_target(word: Word, value: Scalar | None) -> str:
    match value:
        case "0":
            return "`0` names this shell's whole process group"
        case "-1":
            return "`-1` broadcasts to every process you own"
        case str() if NEGATIVE_TARGET.fullmatch(value):
            return f"`{clip(value, 40)}` addresses negative process group {clip(value[1:], 40)}, every process in it"
        case str() if value.startswith("%"):
            return f"`{clip(value, 40)}` is a job spec the shell resolves, not a literal pid"
        case _:
            return f"target `{clip(word.raw, 40)}` is not a literal positive pid"


def pid_verdict(pid: int, spelling: str, facts: Facts, fix: str) -> str | None:
    ownership = facts.ownership
    if isinstance(ownership, Unreadable):
        return unresolvable(spelling, ownership.reason, fix)
    if (row := ownership.table.rows.get(pid)) is None:
        return (
            f"BLOCKED: pid {pid} is not in the current process table, so it is stale or recycled and would reach an "
            f"unrelated process. {fix}"
        )
    if pid in ownership.protected:
        return (
            f"BLOCKED: {describe(row)} is {process_class(row) or 'an ancestor of a protected process'}, which no "
            "session may signal, stop, reprioritize, or restart. Ask the owner to end it."
        )
    if facts.started_here(row):
        return None
    agent = ownership.table.nearest(row.ppid, is_agent)
    if facts.spawned_here(row) and agent in (None, ownership.owner):
        return None
    if agent is None and scratch_orphan(row):
        logger.bind(pid=pid, command=row.command, started=row.started).warning(
            "allowing a scratch orphan that predates spawn records"
        )
        return None
    if ownership.owner is None:
        return (
            f"BLOCKED: ownership of {describe(row)} is unproven because the guard cannot resolve this session's own "
            f"agent process. {fix}"
        )
    holder = f"under {agent.argv0} {agent.pid}" if agent is not None else "with no agent ancestor to vouch for it"
    return (
        f"BLOCKED: {describe(row)} runs {holder}, and no recorded per-task creation identity ties it to this task. "
        "Let it finish, or ask the owner to end it."
    )


def literal_head(call: Call) -> bool:
    words = call.command.words
    return bool(words) and words[0].value is not None and not expands_name(words[0])


def expands_name(head: Word) -> bool:
    return (
        head.expandable
        and head.value is not None
        and head.value not in TEST_BUILTINS
        and ("{" in head.value or glob.has_magic(head.value))
    )


def guarded_token(token: str) -> bool:
    name = PurePath(QUOTING_CHARS.sub("", token)).name.translate(FOLD_TABLE)
    return GUARDED_WORD.fullmatch(name) is not None


def guarded_program_token(token: str) -> str | None:
    name = QUOTING_CHARS.sub("", PurePath(token).name if "/" in token else token)
    return None if (match := GUARDED_PROGRAM.search(name)) is None else match.group(1)


def names_guarded_program(text: str) -> str | None:
    return next((name for token in ARG_TOKEN_BREAK.split(text) if (name := guarded_program_token(token))), None)


def guarded_word_token(token: str) -> str | None:
    name = QUOTING_CHARS.sub("", PurePath(token).name if "/" in token else token).translate(FOLD_TABLE)
    return None if (match := GUARDED_WORD.search(name)) is None else match.group(1)


def names_guarded_word(text: str) -> str | None:
    return next((name for token in ARG_TOKEN_BREAK.split(text) if (name := guarded_word_token(token))), None)


def names_program(resolution: Resolution) -> bool:
    return isinstance(resolution, Resolved) and all(
        candidate.split() and guarded_word_token(program_name(candidate)) is None for candidate in resolution.candidates
    )


def guarded_argument(call: Call, *, named: bool) -> str | None:
    if call.substituted:
        return "a command substitution"
    for word in call.command.words[1:]:
        if word.value is not None:
            texts: tuple[str, ...] = (word.value,)
        else:
            match call.resolve(word):
                case Resolved(candidates, _):
                    texts = candidates
                case Unresolved(None) if named:
                    continue
                case Unresolved(None):
                    return f"`{clip(word.raw, 40)}`"
                case Unresolved(source):
                    if names_guarded_word(source) is not None:
                        return f"`{clip(word.raw, 40)}`"
                    continue
        if any(guarded_token(token) for text in texts for token in ARG_TOKEN_BREAK.split(text)):
            return f"`{clip(word.raw, 40)}`"
    return None


def head_reason(call: Call) -> str | None:
    if not (words := call.command.words) or (head := words[0]).value is not None and not expands_name(head):
        return None
    spelling = spell(call)
    if expands_name(head):
        return (
            f"BLOCKED: `{spelling}` runs a command whose name the shell expands at run time "
            f"(`{clip(head.raw, 40)}`), so the guard cannot tell what runs. Spell the command name literally."
        )
    resolution = call.resolve(head)
    if (argument := guarded_argument(call, named=names_program(resolution))) is not None:
        return (
            f"BLOCKED: `{spelling}` runs a command named at run time (`{clip(head.raw, 40)}`) with {argument} "
            "among its arguments, which runs instead if the name expands to nothing. Spell the command name literally."
        )
    match resolution:
        case Resolved() if (spelled := spellings(call)) is not None and spelled.count > VARIANT_LIMIT:
            return (
                f"BLOCKED: `{spelling}` runs a command named at run time (`{clip(head.raw, 40)}`) that expands to "
                f"more than {VARIANT_LIMIT} command lines, more than the guard reads. Spell the command name literally."
            )
        case Resolved():
            return None
        case Unresolved(None):
            return (
                f"BLOCKED: `{spelling}` runs a command named at run time (`{clip(head.raw, 40)}`), so the guard "
                "cannot tell what runs. Spell the command name literally."
            )
        case Unresolved(source) if source:
            return (
                f"BLOCKED: `{spelling}` runs a command named at run time (`{clip(head.raw, 40)}`) built from text "
                f"the guard cannot read (`{clip(source, 40)}`). Spell the command name literally."
            )
        case _:
            return None


def emitted_piece(piece: str) -> str:
    return piece if UNQUOTABLE.search(piece) is None else shlex.quote(piece)


def emitted(resolution: Resolved, candidate: str) -> str:
    return " ".join(map(emitted_piece, candidate.split())) if resolution.splittable else shlex.quote(candidate)


@dataclass(frozen=True, slots=True)
class Spelled:
    """Each source word's candidate spellings, plus the names the words kept unresolved still reference."""

    words: tuple[tuple[str, ...], ...]
    unread: frozenset[str]

    @property
    def count(self) -> int:
        return prod(map(len, self.words))


@dataclass(frozen=True, slots=True)
class Variants:
    texts: tuple[str, ...]
    unread: frozenset[str]


def spellings(call: Call) -> Spelled | None:
    if not (words := call.source.words) or call.substituted:
        return None
    spelled: list[tuple[str, ...]] = []
    unread: set[str] = set()
    resolved = False
    for word in words:
        if word.value is not None:
            spelled.append((word.raw,))
            continue
        match call.resolve(word):
            case Resolved(candidates, _) as resolution:
                spelled.append(tuple(emitted(resolution, candidate) for candidate in candidates))
                resolved = True
            case Unresolved(_):
                spelled.append((word.raw,))
                unread |= references(word.raw)
    return Spelled(tuple(spelled), frozenset(unread)) if resolved else None


def variants(call: Call) -> Variants | None:
    if (
        not call.command.words
        or (call.command.words[0].value is None and head_reason(call) is not None)
        or (spelled := spellings(call)) is None
        or spelled.count > VARIANT_LIMIT
    ):
        return None
    texts = tuple(" ".join(part for part in combination if part) for combination in product(*spelled.words))
    return Variants(texts, spelled.unread)


def first_operand(call: Call) -> Word | None:
    return next((word for word in call.command.words[1:] if word.value is None or not word.value.startswith("-")), None)


def applescripts(call: Call) -> list[str] | None:
    arguments = OSASCRIPT.bind(call)
    statements = arguments.values.get("statement", ())
    if None in statements:
        return None
    return [str(statement) for statement in statements] or ([] if arguments.values.get("program") else [call.cmd.raw])


def shell_scripts(scripts: list[str]) -> Iterator[str | None]:
    for script in scripts:
        for match in DO_SHELL_SCRIPT.finditer(script):
            yield None if (carried := match.group(1)) is None else APPLESCRIPT_ESCAPE.sub(r"\1", carried)


def payloads(call: Call) -> Iterator[tuple[str, str]]:
    match call.name:
        case "trap" if (action := first_operand(call)) is not None and action.value is not None:
            yield action.value, f"the trap action in `{spell(call)}`"
        case "osascript" if (scripts := applescripts(call)) is not None:
            for script in shell_scripts(scripts):
                if script is not None:
                    yield script, f"the `do shell script` in `{spell(call)}`"
        case _:
            return


def candidate_texts(evt: BaseHookEvent) -> Iterator[str]:
    if isinstance(evt.input, BashCall):
        yield evt.input.command
    elif evt.tool_name not in {"Workflow", "Skill"}:
        yield from command_texts(evt.input.raw)


def names_a_guarded_program(evt: BaseHookEvent) -> bool:
    return any(names_guarded(text) for text in candidate_texts(evt))


@dataclass(frozen=True, slots=True)
class Unparsed:
    source: str
    program: str


@dataclass(slots=True)
class Scan:
    LOCK: ClassVar[threading.Lock] = threading.Lock()
    CACHE: ClassVar[OrderedDict[int, Scan]] = OrderedDict()
    raw: object
    facts: Facts = field(default_factory=Facts)
    calls: list[Call] = field(default_factory=list)
    respelled: list[Call] = field(default_factory=list)
    unparsed: list[Unparsed] = field(default_factory=list)
    guard: threading.Lock = field(default_factory=threading.Lock)
    scanned: bool = False

    @classmethod
    def of(cls, evt: BaseHookEvent) -> Scan:
        with cls.LOCK:
            scan = cls.CACHE.get(id(evt._raw))
            if scan is None or scan.raw is not evt._raw:
                facts = Facts(evt._raw.get("session_id"), Spawns.load(evt).records.values())
                scan = cls.CACHE[id(evt._raw)] = cls(evt._raw, facts)
            cls.CACHE.move_to_end(id(evt._raw))
            while len(cls.CACHE) > SCAN_CACHE_LIMIT:
                cls.CACHE.popitem(last=False)
        with scan.guard:
            if not scan.scanned:
                for text in filter(names_guarded, candidate_texts(evt)):
                    scan.read(text.encode(errors="replace").decode(), f"this `{evt.tool_name}` payload", evt.cwd)
                scan.scanned = True
        return scan

    def read(
        self,
        text: str,
        source: str,
        cwd: Path | str | None,
        unread: frozenset[str] = frozenset(),
        *,
        respelled: bool = False,
    ) -> None:
        line = safe_parse_command_line(text)
        bindings = dict.fromkeys(unread, Unknown(None))
        if not (calls := () if line is None else Cmd(line, raw=text, cwd=cwd, bindings=bindings).calls()):
            if (named := names_guarded_program(text)) is not None:
                self.unparsed.append(Unparsed(source, named))
            return
        for call in calls:
            if (resolved := variants(call)) is not None:
                for variant in resolved.texts:
                    self.read(variant, source, call.cwd, unread | resolved.unread, respelled=True)
                continue
            self.calls.append(call)
            if respelled:
                self.respelled.append(call)
            if literal_head(call):
                for payload, origin in payloads(call):
                    self.read(payload, origin, call.cwd, respelled=respelled)

    @property
    def literal_calls(self) -> list[Call]:
        return [call for call in self.calls if literal_head(call)]


def orca_json(argv: tuple[str, ...], *path: str, unset: tuple[str, ...] = ()) -> Any:
    if isinstance(shown := probe(argv, unset=unset), Unreadable):
        return shown
    try:
        return reduce(lambda node, key: node[key], path, json.loads(shown))
    except (ValueError, TypeError, KeyError):
        return Unreadable(f"`{' '.join(argv[:3])}` printed no {path[-1]}")


def created_handle(text: str) -> str | None:
    try:
        return json.loads(text)["result"]["terminal"]["handle"]
    except (ValueError, TypeError, KeyError):
        return None


def receipt_names(lane: str, handle: str, start: datetime, end: datetime) -> bool:
    if LANE_NAME.fullmatch(lane) is None:
        return False
    return any(
        start <= datetime.fromtimestamp(receipt.stat().st_mtime, UTC) <= end
        and created_handle(receipt.read_text()) == handle
        for receipt in (Path.home() / LAUNCH_RECEIPTS).glob(f"*/{lane}.terminal.json")
    )


def bare_orca(call: Call) -> bool:
    head = call.source.words[0] if call.source.words else None
    return head is not None and head.value == "orca" and not call.source.env and not call.substituted


def created_by(use: Any, handle: str, cwd: Path | None) -> BashCall | None:
    if not isinstance(bash := use.call, BashCall) or use.result is None:
        return None
    line = safe_parse_command_line(bash.command)
    calls = () if line is None else Cmd(line, raw=bash.command, cwd=cwd).calls()
    if len(calls) == 1 and bare_orca(calls[0]):
        values = ORCA.bind(calls[0]).values
        if (values.get("group"), values.get("verb")) == (("terminal",), ("create",)):
            return bash if created_handle(use.result.content) == handle else None
    start, end = use.ts - RECEIPT_SLACK, (use.result_ts or use.ts) + RECEIPT_SLACK
    launched = f"terminal={handle}" in use.result.content and any(
        receipt_names(call.args[0], handle, start, end) for call in calls if call.name == "orca-launch.sh" and call.args
    )
    return bash if launched else None


def runs_once(call: Call, scan: Scan) -> bool:
    occurrence = call.occurrence
    return (
        occurrence.nesting == 0
        and occurrence.index == 0
        and occurrence.line.raw.strip().startswith(call.source.raw)
        and all(other is call or other.occurrence.prev_op == "|" for other in scan.calls)
    )


def worker_of(handle: str) -> dict[str, Any] | Unreadable | None:
    cursor: tuple[str, ...] = ()
    while True:
        argv = ("orca", "orchestration", "worker-list", "--limit", str(WORKER_PAGE), *cursor, "--json")
        if isinstance(page := orca_json(argv, "result", unset=("ORCA_TERMINAL_HANDLE",)), Unreadable):
            return page
        try:
            if page["scope"]["source"] != "all":
                return Unreadable("Orca scoped its worker list to one Run")
            holder = next(
                (
                    row
                    for row in page["workers"]
                    if handle in (row.get("agentTerminalHandle"), (row.get("resource") or {}).get("terminalHandle"))
                ),
                None,
            )
            after = (page.get("page") or {}).get("nextCursor")
        except (TypeError, KeyError):
            return Unreadable("Orca's worker list has no workers")
        if holder is not None or not after:
            return holder
        cursor = ("--cursor", after)


def idle_screen(tail: list[str]) -> bool:
    if any(marker in line for line in tail for marker in BUSY):
        return False
    lines = [line.strip() for line in tail if not BORDER.match(line)]
    prompt = next(
        (
            index
            for index in range(len(lines) - 1, max(len(lines) - PROMPT_WINDOW, 0) - 1, -1)
            if lines[index] == CLAUDE_PROMPT or lines[index].startswith(CODEX_PROMPT)
        ),
        None,
    )
    if prompt is None:
        return False
    said = [line for line in lines[:prompt] if not DONE_MARKER.match(line)]
    return not said or not said[-1].endswith("?")


def idle(handle: str) -> bool | Unreadable:
    wait = ("orca", "terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", str(IDLE_WAIT_MS))
    if isinstance(satisfied := orca_json((*wait, "--json"), "result", "wait", "satisfied"), Unreadable):
        return satisfied
    read = ("orca", "terminal", "read", "--terminal", handle, "--screen", "--limit", str(SCREEN_LINES), "--json")
    if isinstance(screen := orca_json(read, "result", "terminal"), Unreadable):
        return screen
    return satisfied is True and screen.get("source") == "screen" and idle_screen(screen.get("tail") or [])


def tab_nodes(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if "tabId" in node and "panes" in node:
            yield node
        for child in node.values():
            yield from tab_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from tab_nodes(child)


def lone_pane(handle: str) -> bool | Unreadable:
    if isinstance(
        shown := orca_json(("orca", "terminal", "show", "--terminal", handle, "--json"), "result"), Unreadable
    ):
        return shown
    try:
        tab, worktree = shown["terminal"]["tabId"], shown["terminal"]["worktreePath"]
    except (TypeError, KeyError):
        return Unreadable("Orca shows no tab for the terminal")
    listing = ("orca", "terminal", "list", "--worktree", f"path:{worktree}", "--include-visual-layouts", "--json")
    if isinstance(layouts := orca_json(listing, "result", "visualLayouts"), Unreadable):
        return layouts
    panes = next((node["panes"] for node in tab_nodes(layouts) if node["tabId"] == tab), None)
    if not isinstance(panes, dict):
        return Unreadable("Orca's layout has no such tab")
    return panes.get("type") == "terminal" and panes.get("handle") == handle


@dataclass(frozen=True, slots=True)
class CreatedHere:
    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        handle = action.scope["terminal"]
        found = next(
            (
                (use, bash)
                for turn in evt.ctx.t.turns
                for use in turn.tool_uses
                if (bash := created_by(use, handle, evt.cwd)) is not None
            ),
            None,
        )
        if found is None:
            return []
        use, bash = found
        holder = worker_of(handle)
        ready = idle(handle) if holder is None else False
        if holder is not None or ready is not True:
            dispatch = holder.get("dispatchId") if isinstance(holder, dict) else holder
            logger.bind(terminal=handle, dispatch=dispatch, idle=ready).info("a terminal this session created is busy")
            return []
        return [
            Evidence(
                id=f"created:{handle}",
                source="created",
                quote=clip(bash.command, 200),
                said_at=use.result_ts or use.ts,
                detail=f"this session created {handle}; no dispatch names it and its agent idles at a prompt",
                key=f"created:{evt.session_id}/{evt.agent_id or 'main'}/{handle}",
                live=True,
            )
        ]


def settled_worker(handle: str) -> dict[str, Any] | None:
    worker = worker_of(handle)
    if not isinstance(worker, dict) or worker.get("dispatchStatus") not in SETTLED or idle(handle) is not True:
        return None
    return worker


def coordinates(evt: BaseHookEvent, run: str | None) -> bool:
    caller = reqenv.getenv("ORCA_TERMINAL_HANDLE")
    if evt.agent_id is not None or not caller or not run:
        return False
    shown = ("orca", "orchestration", "run-show", "--id", run, "--json")
    return orca_json(shown, "result", "run", "coordinator_handle") == caller


def settled_close(evt: BaseHookEvent, handle: str, tab: bool) -> Proposal | None:
    if (worker := settled_worker(handle)) is None or not coordinates(evt, run := worker.get("runId")):
        return None
    dispatch, status = worker["dispatchId"], worker["dispatchStatus"]
    stage = ((worker.get("projection") or {}).get("stage") or {}).get("detail")
    return Proposal(
        scope={},
        payload={"terminal": handle, "tab": tab, "dispatch": dispatch, "run": run, "status": status},
        summary=f"the coordinator of run {run} closes terminal {handle}, whose dispatch {dispatch} is {status} "
        f"({stage}) and whose agent idles at a prompt",
    )


def still_settled(action: Proposal) -> bool:
    worker = settled_worker(action.payload["terminal"])
    return worker is not None and worker["dispatchId"] == action.payload["dispatch"]


def spawned(use: Any, task: str) -> bool:
    if use.call.name not in SPAWN_TOOLS or use.result is None:
        return False
    result = use.result.tool_use_result
    return isinstance(result, dict) and result.get("status") == "teammate_spawned" and result.get("teammate_id") == task


@dataclass(frozen=True, slots=True)
class OwnTeammate:
    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        task = action.scope["task"]
        if TEAMMATE_TASK.fullmatch(task) is None:
            return []
        if recorded := children(evt, "spawn", task):
            return recorded
        found = next((use for turn in evt.ctx.t.turns for use in turn.tool_uses if spawned(use, task)), None)
        if found is None:
            return []
        return [
            Evidence(
                id=f"teammate:{task}",
                source="teammate",
                quote=task,
                said_at=found.result_ts or found.ts,
                detail=f"this agent's own transcript records spawning {task} as its teammate",
                key=f"teammate:{evt.session_id}/{evt.agent_id or 'main'}/{task}",
                live=True,
            )
        ]


@dataclass(frozen=True, slots=True)
class OwnShell:
    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        return children(evt, "shell", action.scope["task"])


@dataclass(frozen=True, slots=True)
class OwnerNaming:
    """The owner's words and answers that name the action's *key* target, or the lane its task id begins with."""

    key: str
    sources: tuple[EvidenceSource, ...] = (Asked(), OwnerWords())

    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        target = action.scope[self.key]
        named = {target, target.partition("@session-")[0]} - {""}
        return [
            item
            for source in self.sources
            for item in source.collect(evt, action)
            if any(names(f"{item.quote}\n{item.detail}", name) for name in named)
        ]


def rulings_naming(key: str) -> Rulings:
    return Rulings(search=lambda action: action.scope[key])


TERMINAL_CLOSE = Grants(
    "sessions.close",
    ("terminal",),
    evidence=(rulings_naming("terminal"), CreatedHere()),
    replay=RETRY_WINDOW,
    would_allow="Close an idle terminal this session created, or have the owner name the terminal id in a "
    "cc-notes answer before the closing session starts.",
    hook="sessions",
)
LAUNCHD_STOP = Grants(
    "sessions.launchctl",
    ("service",),
    evidence=(rulings_naming("service"),),
    replay=RETRY_WINDOW,
    would_allow="Have the owner name the service label in a cc-notes answer before the acting session starts.",
    hook="sessions",
)
TASK_STOP = Grants(
    "sessions.task-stop",
    ("task",),
    evidence=(OwnTeammate(), OwnShell(), rulings_naming("task")),
    replay=RETRY_WINDOW,
    would_allow="Stop only a teammate this agent spawned, by its `<name>@session-<id>` task id, or a background shell "
    "this agent started, or have the owner name the task id in a cc-notes answer before the stopping session starts.",
    hook="sessions",
)


def owner_named(kind: str, key: str, target: str) -> Grants:
    return Grants(
        f"{kind}.owner",
        (key,),
        judge=Judge(rules=OWNER_NAMED_RULES),
        evidence=(OwnerNaming(key),),
        ttl=OWNER_NAMED_TTL,
        replay=RETRY_WINDOW,
        would_allow=f"Ask the owner to name the {target} and say to end it; their answer lifts this block once.",
        hook="sessions",
    )


TERMINAL_CLOSE_NAMED = owner_named("sessions.close", "terminal", "terminal")
LAUNCHD_STOP_NAMED = owner_named("sessions.launchctl", "service", "service")
TASK_STOP_NAMED = owner_named("sessions.task-stop", "task", "task")
OWNER_NAMED = {
    TERMINAL_CLOSE.kind: TERMINAL_CLOSE_NAMED,
    LAUNCHD_STOP.kind: LAUNCHD_STOP_NAMED,
    TASK_STOP.kind: TASK_STOP_NAMED,
}
SETTLED_CLOSE = Grants(
    "sessions.close-settled",
    (),
    judge=Judge(rules=SETTLED_CLOSE_RULES),
    evidence=(StandingRulings(CLASS_RULINGS),),
    mint=None,
    ttl=None,
    would_allow="Close only an idle terminal whose dispatch Orca records as settled, under a standing owner decision "
    "recorded in cc-notes before the closing session.",
    hook="sessions",
)


@dataclass(frozen=True, slots=True)
class Ungranted:
    message: str
    reason: str
    detail: str = ""


def spawning_root(evt: BaseHookEvent) -> bool:
    return evt.agent_id is None and evt.ctx.root_path is None


def refusal(message: str, hook: str, refused: Denied) -> Ungranted:
    return Ungranted(message, refused.message if refused.reason else "", f"{hook}: {refused.explained}")


def owner_lift(evt: BaseHookEvent, grants: Grants, action: Proposal, ungranted: Ungranted) -> Ungranted | None:
    """``None`` when the owner named *action*'s target in their own words, for the spawning root only, once."""
    if (named := OWNER_NAMED.get(grants.kind)) is None or not spawning_root(evt):
        return ungranted
    if isinstance(verdict := named.decide(evt, action), Allowed):
        return None
    if not verdict.reason:
        return ungranted
    return Ungranted(ungranted.message, verdict.message, f"{ungranted.detail} {verdict.explained}".strip())


def lift(evt: BaseHookEvent, grants: Grants, action: Proposal, message: str, *, owner: bool = True) -> Ungranted | None:
    """``None`` when a grant lifts the block, else the block with why nothing covered it.

    With *owner*, a kind with an owner-named lift tries it after its own evidence.
    """
    if isinstance(verdict := grants.decide(evt, action), Allowed):
        return None
    ungranted = refusal(message, grants.hook, verdict)
    return owner_lift(evt, grants, action, ungranted) if owner else ungranted


def block_first(evt: ToolRewriteEvent, messages: Iterable[str | Ungranted | None]) -> HookResult | None:
    match next((message for message in messages if message is not None), None):
        case Ungranted(message, reason, detail):
            return evt.block(reason or message, system_message=detail or None)
        case str() as message:
            return evt.block(message)
        case _:
            return None


guard = partial(
    on,
    Event.PreToolUse | Event.PermissionRequest,
    only_if=[LambdaCondition(names_a_guarded_program)],
    respect_gitignore=False,
    skip_planning_agents=False,
    mandatory=True,
)
