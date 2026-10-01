from __future__ import annotations

import glob
import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from functools import cached_property, partial, reduce
from typing import TYPE_CHECKING

from cc_transcript.tools import BashCall

from captain_hook import Event, Input, LambdaCondition, on
from captain_hook.cmd import Cmd
from captain_hook.command_schemas import OSASCRIPT
from captain_hook.dispatch import SYNC_DEADLINE_MARGIN_SECONDS, collect_budget
from captain_hook.guard_literal import QUOTING_CHARS, names_guarded
from captain_hook.util import proc, reqenv
from captain_hook.util.payload import command_texts
from captain_hook.util.shell import safe_parse_command_line

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

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
VERIFY = "Verify a pid you started with `ps -o pid,ppid,pgid,lstart,command -p <pid>`"
KILL_FIX = f"{VERIFY} and run `kill <pid>` alone."
RENICE_FIX = f"{VERIFY} and run `renice -n <priority> -p <pid>` alone."
SPELLING_LIMIT = 60
LAST_SCAN = threading.local()
INLINE_TABLE = (
    "    1     0     1    0 Thu Jan  1 00:00:00 2026 /sbin/launchd\n"
    "  900     1   900  501 Thu Jan  1 00:00:00 2026 /Applications/Captain Hook.app/Contents/Helpers/capt-hookd serve\n"
    " 1445     1  1445  501 Thu Jan  1 00:00:00 2026 /Applications/Orca.app/Contents/MacOS/Orca\n"
    " 1743  1445  1743  501 Thu Jan  1 00:00:00 2026 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app/"
    "Contents/MacOS/Orca Helper /Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js\n"
    "14545  1743 14545    0 Thu Jan  1 00:00:00 2026 /usr/bin/login -flpq dev /bin/bash --noprofile --norc -p -c "
    "orca-tcc-login /bin/zsh\n"
    "14550 14545 14550  501 Thu Jan  1 00:00:00 2026 -/bin/zsh -l\n"
    "14575 14550 14575  501 Thu Jan  1 00:00:00 2026 claude --dangerously-skip-permissions\n"
    "27103 14575 27103  501 Thu Jan  1 00:00:00 2026 /bin/zsh -c capt-hook run PreToolUse\n"
    "31337 14575 31337  501 Thu Jan  1 00:00:00 2026 sleep 60\n"
)

guarded = partial(Input, commands={"ps -A -ww -o": INLINE_TABLE})


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


def describe(row: ProcessRow) -> str:
    return f"pid {row.pid} (`{clip(row.command, 40)}`)"


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


class Facts:
    @cached_property
    def ownership(self) -> Ownership | Unreadable:
        budget = collect_budget(SYNC_DEADLINE_MARGIN_SECONDS)
        if budget is not None and budget < 0.5:
            return Unreadable("the caller deadline is too close to read the process table")
        table = proc.process_table(timeout=2.0 if budget is None else min(2.0, budget - 0.25))
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


def pid_verdict(pid: int, spelling: str, facts: Facts, fix: str) -> str:
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
    if ownership.owner is None:
        return (
            f"BLOCKED: ownership of {describe(row)} is unproven because the guard cannot resolve this session's own "
            f"agent process. {fix}"
        )
    agent = ownership.table.nearest(row.ppid, is_agent)
    holder = f"under {agent.argv0} {agent.pid}" if agent is not None else "with no agent ancestor to vouch for it"
    return (
        f"BLOCKED: {describe(row)} runs {holder}, and no recorded per-task creation identity ties it to this task. "
        "Stop a background task you started with the harness's stop tool, or wait for it to exit."
    )


def literal_head(call: Call) -> bool:
    words = call.command.words
    return bool(words) and words[0].value is not None and not expands_name(words[0])


def expands_name(head: Word) -> bool:
    return head.expandable and head.value is not None and ("{" in head.value or glob.has_magic(head.value))


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
    raw: object
    facts: Facts = field(default_factory=Facts)
    calls: list[Call] = field(default_factory=list)
    unparsed: list[Unparsed] = field(default_factory=list)

    @classmethod
    def of(cls, evt: BaseHookEvent) -> Scan:
        last: Scan | None = getattr(LAST_SCAN, "scan", None)
        if last is not None and last.raw is evt._raw:
            return last
        scan = cls(evt._raw)
        for text in filter(names_guarded, candidate_texts(evt)):
            scan.read(text.encode(errors="replace").decode(), f"this `{evt.tool_name}` payload", evt.cwd)
        LAST_SCAN.scan = scan
        return scan

    def read(self, text: str, source: str, cwd: Path | str | None) -> None:
        line = safe_parse_command_line(text)
        if not (calls := () if line is None else Cmd(line, raw=text, cwd=cwd).calls()):
            if (named := GUARDED_PROGRAM.search(QUOTING_CHARS.sub("", text))) is not None:
                self.unparsed.append(Unparsed(source, named.group(1)))
            return
        for call in calls:
            self.calls.append(call)
            if literal_head(call):
                for payload, origin in payloads(call):
                    self.read(payload, origin, call.cwd)

    @property
    def literal_calls(self) -> list[Call]:
        return [call for call in self.calls if literal_head(call)]


def block_first(evt: ToolRewriteEvent, messages: Iterable[str | None]) -> HookResult | None:
    message = next((message for message in messages if message is not None), None)
    return None if message is None else evt.block(message)


guard = partial(
    on,
    Event.PreToolUse | Event.PermissionRequest,
    only_if=[LambdaCondition(names_a_guarded_program)],
    respect_gitignore=False,
    skip_planning_agents=False,
    mandatory=True,
)
