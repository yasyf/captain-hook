from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from functools import cached_property, partial, reduce
from pathlib import PurePath
from textwrap import shorten
from typing import TYPE_CHECKING

from cc_transcript.command import PAYLOAD_DEPTH_LIMIT
from cc_transcript.tools import BashCall

from captain_hook import Allow, Block, Event, Input, LambdaCondition, on
from captain_hook.cmd import Cmd
from captain_hook.command_schemas import KILL, LAUNCHCTL, ORCA, OSASCRIPT, PMSET, RENICE, SOFTWAREUPDATE, TMUX
from captain_hook.dispatch import SYNC_DEADLINE_MARGIN_SECONDS, collect_budget
from captain_hook.guard_literal import QUOTING_CHARS, names_guarded
from captain_hook.util import proc, reqenv
from captain_hook.util.payload import command_texts
from captain_hook.util.shell import SHELLS, safe_parse_command_line

if TYPE_CHECKING:
    from collections.abc import Iterator
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
FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
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
ORCA_GROUPS = frozenset(
    {
        "account",
        "agent",
        "agent-context",
        "artifacts",
        "automations",
        "capture",
        "claude-teams",
        "computer",
        "cookie",
        "diagnostics",
        "emulator",
        "environment",
        "host",
        "open",
        "orchestration",
        "project",
        "repo",
        "search",
        "serve",
        "skills",
        "status",
        "storage",
        "tab",
        "terminal",
        "worktree",
    }
)
ORCA_ENDINGS = {
    "terminal": frozenset({"close", "stop"}),
    "worktree": frozenset({"rm", "remove", "delete"}),
    "orchestration": frozenset({"worker-stop", "worker-release", "coordinator-stop", "run-stop"}),
}
ORCA_PAYLOADS = {
    "hotkey": "key",
    "press-key": "key",
    "type-text": "text",
    "paste-text": "text",
    "set-value": "value",
    "perform-secondary-action": "action",
}
KEY_ALIASES = {
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    "escape": "esc",
}
END_OF_SESSION_CHORDS = frozenset(
    frozenset(chord)
    for chord in (
        ("cmd", "q"),
        ("ctrl", "q"),
        ("cmd", "w"),
        ("cmd", "shift", "w"),
        ("cmd", "alt", "esc"),
        ("ctrl", "c"),
        ("ctrl", "d"),
    )
)
END_OF_SESSION_ACTION = re.compile(r"(?i)\b(quit|close|kill|stop|terminate|interrupt|remove|delete)\b")
END_OF_SESSION = frozenset(
    {"exit", "/exit", "/quit", "logout", "\x03", "\x04", "^c", "^d", "c-c", "c-d", "\\x03", "\\x04"}
)
LAUNCHCTL_ENDINGS = frozenset(
    {"bootout", "kill", "kickstart", "stop", "remove", "unload", "disable", "reboot", "asuser", "bsexec"}
)
APPLESCRIPT_ENDING = re.compile(r'(?i)\b(quit|log ?out|restart|shut ?down|sleep)\b|keystroke\s+"q"')
PMSET_ENDINGS = frozenset({"sleepnow", "restart", "halt", "sleep"})
PMSET_SCHEDULES = frozenset({"shutdown", "restart", "sleep", "poweroff"})
TMUX_ENDINGS = frozenset({"kill-server", "kill-session", "kill-pane", "kill-window"})
POLICY = "AGENTS.md § Protect Existing Sessions"
OWNER_RUNS_IT = "If the owner explicitly authorized ending this exact target, ask them to run it themselves."
VERIFIED_KILL = (
    "Signal only a process you started, by literal pid: capture the pid, verify it with "
    "`ps -o pid,ppid,pgid,lstart,command -p <pid>`, then `kill <literal pid>` in its own call."
)
VERIFIED_RENICE = (
    "Reprioritize only a process you started, by literal pid: capture the pid, verify it with "
    "`ps -o pid,ppid,pgid,lstart,command -p <pid>`, then `renice -n <priority> -p <literal pid>` in its own call."
)
HARNESS_STOP = (
    "To stop a background Bash task this session started, use the harness's stop tool for that task; otherwise "
    "wait for the process to exit or ask the owner to end it."
)
INLINE_TABLE = (
    "    1     0     1    0 Wed Sep 30 05:54:44 2026 /sbin/launchd\n"
    "  900     1   900  501 Wed Sep 30 05:55:00 2026 /Users/yasyf/Applications/Captain Hook.app/Contents/Helpers/"
    "capt-hookd serve\n"
    " 1445     1  1445  501 Wed Sep 30 05:58:20 2026 /Applications/Orca.app/Contents/MacOS/Orca\n"
    " 1743  1445  1743  501 Wed Sep 30 05:58:30 2026 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app/"
    "Contents/MacOS/Orca Helper /Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js\n"
    "14545  1743 14545    0 Wed Sep 30 06:01:00 2026 /usr/bin/login -flpq yasyf /bin/bash --noprofile --norc -p -c "
    "orca-tcc-login /opt/homebrew/bin/fish\n"
    "14550 14545 14550  501 Wed Sep 30 06:01:01 2026 -/opt/homebrew/bin/fish -l\n"
    "14575 14550 14575  501 Wed Sep 30 06:01:05 2026 claude --dangerously-skip-permissions\n"
    "27103 14575 27103  501 Wed Sep 30 06:20:00 2026 /opt/homebrew/bin/zsh -c capt-hook run PreToolUse\n"
    "31337 14575 31337  501 Wed Sep 30 06:30:00 2026 sleep 60\n"
)

guarded = partial(Input, commands={"ps -A -ww -o": INLINE_TABLE})


def nested(depth: int, payload: str, *, wrapper: str = "bash -c") -> str:
    return reduce(lambda acc, _: f"{wrapper} {shlex.quote(acc)}", range(depth), payload)


def denial(*parts: str) -> Denial:
    return Denial(" ".join((*parts, OWNER_RUNS_IT)))


def process_class(row: ProcessRow) -> str | None:
    if proc.is_claude(row.command.split()):
        return PROCESS_CLASSES["claude"]
    if (label := PROCESS_CLASSES.get(row.argv0)) is not None:
        return label
    return next((label for marker, label in COMMAND_MARKERS.items() if marker in row.command), None)


def is_agent(row: ProcessRow) -> bool:
    return proc.is_claude(row.command.split()) or row.argv0 in {"claude", "codex"}


def describe(row: ProcessRow) -> str:
    return (
        f"pid {row.pid} (`{shorten(row.command, 120, placeholder='…')}`, started {row.started:%Y-%m-%d %H:%M:%S} UTC)"
    )


@dataclass(frozen=True, slots=True)
class Denial:
    message: str


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


def spell(call: Call) -> str:
    return call.source.raw


def unresolvable(spelling: str, reason: str, *, path: str) -> Denial:
    return denial(
        f"BLOCKED: `{spelling}` cannot be checked against the process table because {reason}, and an unverified "
        f"target may be another session's process or a protected host ({POLICY}).",
        path,
    )


def hidden_behind(arguments: Arguments) -> str:
    if arguments.unread:
        return f"`{arguments.unread[0].raw}`, an option the guard cannot read"
    return "a command substitution"


def queries_path(call: Call) -> bool:
    return "command" in call.wrappers and not {"-v", "-V"}.isdisjoint(call.source.args)


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
            return f"`{value}` addresses negative process group {value[1:]}, every process in it"
        case str() if value.startswith("%"):
            return f"`{value}` is a job spec the shell resolves, not a literal pid"
        case _:
            return f"target `{word.raw}` is not a literal positive pid"


def unread_reason(call: Call, arguments: Arguments) -> str:
    negative = next(
        (
            word
            for index, word in enumerate(call.command.words[1:])
            if word.value is not None
            and NEGATIVE_TARGET.fullmatch(word.value)
            and (index or not 1 <= int(word.value[1:]) <= 64)
        ),
        None,
    )
    if negative is not None:
        return describe_target(negative, negative.value)
    if arguments.unread:
        return f"it passes `{arguments.unread[0].raw}`, an option the guard cannot read"
    return "it names an option or signal at run time"


def pid_verdict(pid: int, spelling: str, facts: Facts, *, path: str) -> Denial:
    ownership = facts.ownership
    if isinstance(ownership, Unreadable):
        return unresolvable(spelling, ownership.reason, path=path)
    if (row := ownership.table.rows.get(pid)) is None:
        return denial(
            f"BLOCKED: pid {pid} in `{spelling}` is not in the current process table, so it is stale or already "
            f"recycled and would reach an unrelated process ({POLICY}).",
            path,
        )
    if pid in ownership.protected:
        return denial(
            f"BLOCKED: {describe(row)} is {process_class(row) or 'an ancestor of a protected process'}, which no "
            f"session may signal, stop, reprioritize, or restart ({POLICY}).",
            path,
        )
    if (owner := ownership.owner) is None:
        return denial(
            f"BLOCKED: `{spelling}` targets {describe(row)}, but the guard cannot resolve this session's own agent "
            f"process from the hook's ancestry, so ownership of the target is unproven ({POLICY}).",
            path,
        )
    agent = ownership.table.nearest(row.ppid, is_agent)
    holder = f"under {agent.argv0} {agent.pid}" if agent is not None else "with no agent ancestor to vouch for it"
    return denial(
        f"BLOCKED: {describe(row)} may be a disposable child, but no recorded per-task creation identity ties it to "
        f"this task: it runs {holder}, and a pid under this session's {owner.argv0} {owner.pid} can belong to a "
        f"nested agent, a teammate, or a sibling worker ({POLICY}).",
        HARNESS_STOP,
    )


def kill_verdict(call: Call, facts: Facts) -> Denial | None:
    if queries_path(call):
        return None
    spelling = spell(call)
    deny = partial(unresolvable, spelling, path=VERIFIED_KILL)
    arguments = KILL.bind(call)
    if call.substituted:
        return deny("a command substitution supplies its targets at run time")
    if "xargs" in call.wrappers:
        return deny("xargs supplies its targets from stdin")
    if not arguments.complete:
        return deny(unread_reason(call, arguments))
    values = arguments.values
    if "list" in values:
        return None
    targets = tuple(zip(arguments.words.get("targets", ()), values.get("targets", ()), strict=True))
    probing = ("probe" in values and "signal" not in values) or values.get("signal") == ("0",)
    if probing and (
        all(literal_pid(word, value) is not None for word, value in targets)
        or (len(targets) == 1 and QUOTED_WORD.fullmatch(targets[0][0].raw) is not None)
    ):
        return None
    if not targets:
        return deny("a wrapper supplies its targets") if call.wrappers else None
    word, value = targets[0]
    if (pid := literal_pid(word, value)) is None:
        return deny(
            f"{describe_target(word, value)}, so the probe could expand into a real signal; pass one "
            f'double-quoted word (`"{word.raw}"`) or the literal pid'
            if probing
            else describe_target(word, value)
        )
    return pid_verdict(pid, spelling, facts, path=VERIFIED_KILL)


def renice_verdict(call: Call, facts: Facts) -> Denial | None:
    if queries_path(call):
        return None
    spelling = spell(call)
    deny = partial(unresolvable, spelling, path=VERIFIED_RENICE)
    arguments = RENICE.bind(call)
    if call.substituted or "xargs" in call.wrappers:
        return deny("its targets are supplied at run time")
    if not arguments.complete:
        return deny(
            f"it passes `{arguments.unread[0].raw}`, an option the guard cannot read"
            if arguments.unread
            else "it names an option at run time"
        )
    if "scope" in arguments.values:
        return denial(
            f"BLOCKED: `{spelling}` reprioritizes a whole process group or every process of a user, which sweeps "
            f"in other sessions' agents and terminals ({POLICY}).",
            VERIFIED_RENICE,
        )
    pairs = list(zip(arguments.words.get("args", ()), arguments.values.get("args", ()), strict=True))
    if "adjust" not in arguments.values:
        if not pairs or pairs[0][1] is None or not str(pairs[0][1]).lstrip("+-").isdecimal():
            return deny("its priority operand is not a literal number")
        pairs = pairs[1:]
    if not pairs:
        return None
    word, value = pairs[0]
    if (pid := literal_pid(word, value)) is None:
        return deny(describe_target(word, value))
    return pid_verdict(pid, spelling, facts, path=VERIFIED_RENICE)


def orca_bound(arguments: Arguments, name: str) -> str | None:
    return next((f"`{word.raw}`" for word in arguments.words.get(name, ())), None)


def orca_ending(spelling: str, group: str, verb: str, arguments: Arguments) -> Denial:
    values = arguments.values
    match group:
        case "terminal":
            scope = (
                f"terminal {terminal}"
                if (terminal := orca_bound(arguments, "terminal")) is not None
                else f"{'every terminal' if 'all' in values else 'the active terminal'} in worktree {worktree}"
                if (worktree := orca_bound(arguments, "worktree")) is not None
                else "the current tab's terminal"
                if "tab" in values
                else "the current terminal"
            )
            action = f"{'closes' if verb == 'close' else 'stops'} {scope}"
        case "worktree":
            worktree = orca_bound(arguments, "worktree") or "named by its arguments"
            action = f"removes worktree {worktree} and every terminal in it"
        case _:
            action = f"{verb} ends the worker or run {orca_bound(arguments, 'dispatch') or 'named by its arguments'}"
    return denial(
        f"BLOCKED: `{spelling}` {action}, which ends the agent session living there ({POLICY}). Leave sessions "
        "running: read with `orca terminal list|show|read|wait`, and let the owner close, stop, or release them.",
    )


def orca_send_verdict(spelling: str, arguments: Arguments) -> Denial | None:
    values, words = arguments.values, arguments.words
    text = values.get("text", ())
    if "interrupt" in values or any(
        str(payload).strip().casefold() in END_OF_SESSION for payload in text if payload is not None
    ):
        return denial(
            f"BLOCKED: `{spelling}` interrupts or exits the agent in another terminal, which ends that session "
            f"({POLICY}). Send only ordinary text, and let the owner interrupt or exit a session.",
        )
    if None in text:
        return denial(
            f"BLOCKED: `{spelling}` sends text built at run time (`--text {words['text'][text.index(None)].raw}`), "
            f"which could be `exit` or a control byte that ends the receiving session ({POLICY}). Put the literal "
            "text in `--text`.",
        )
    if arguments.unread:
        return denial(
            f"BLOCKED: `{spelling}` passes `{arguments.unread[0].raw}`, an option the guard does not know, so it "
            f"cannot rule out an interrupt ({POLICY}). Drop the option or ask the owner.",
        )
    for word in words.get("rest", ()):
        flag, _, value = word.raw.partition("=")
        if word.value is None and (flag not in {"--terminal", "--worktree"} or QUOTED_WORD.fullmatch(value) is None):
            return denial(
                f"BLOCKED: `{spelling}` passes `{word.raw}`, an argument named at run time that Orca may read as "
                f"`--interrupt` or as the text to send ({POLICY}). Spell it literally, or pass the handle as "
                '`--terminal "$handle"`.',
            )
    loose = next(
        (
            word
            for name, bound in words.items()
            if name != "rest"
            for word in bound
            if word.value is None and QUOTED_WORD.fullmatch(word.raw) is None
        ),
        None,
    )
    if loose is not None:
        return denial(
            f"BLOCKED: `{spelling}` names `{loose.raw}` at run time in a form the shell may split into extra "
            f'words such as `--interrupt` ({POLICY}). Quote it (`"{loose.raw}"`) or spell it literally.',
        )
    return None


def ends_session_key(key: str) -> bool:
    parts = [KEY_ALIASES.get(part, part) for part in (raw.strip() for raw in key.casefold().split("+"))]
    chords = (
        (frozenset(modifier if part == "cmdorctrl" else part for part in parts) for modifier in ("cmd", "ctrl"))
        if "cmdorctrl" in parts
        else (frozenset(parts),)
    )
    return not END_OF_SESSION_CHORDS.isdisjoint(chords)


def ends_session_text(text: str) -> bool:
    return text.strip().casefold() in END_OF_SESSION


def ends_session_action(action: str) -> bool:
    return END_OF_SESSION_ACTION.search(action) is not None


def orca_input_verdict(spelling: str, verb: str, arguments: Arguments) -> Denial | None:
    role = ORCA_PAYLOADS[verb]
    values, words = arguments.values.get(role, ()), arguments.words.get(role, ())
    if None in values or f"{role}_stdin" in arguments.values:
        source = f"--{role} {words[values.index(None)].raw}" if None in values else f"--{role}-stdin"
        return denial(
            f"BLOCKED: `{spelling}` names its {role} at run time (`{source}`), which could quit, close, interrupt, "
            f"or exit the focused session ({POLICY}). Spell it literally.",
        )
    if not values and arguments.unread:
        return denial(
            f"BLOCKED: `{spelling}` hides its {role} behind {hidden_behind(arguments)}, so the guard cannot tell "
            f"whether it quits, closes, interrupts, or exits the focused session ({POLICY}). Spell it literally.",
        )
    match role, next((str(value) for value in values if value is not None), None):
        case ("key", str() as key) if ends_session_key(key):
            return denial(
                f"BLOCKED: `{spelling}` presses `{key}`, which quits, closes, interrupts, or exits the focused "
                f"session ({POLICY}). Press only keys that leave the session running.",
            )
        case ("text" | "value", str() as text) if ends_session_text(text):
            return denial(
                f"BLOCKED: `{spelling}` types `{text}`, which exits the focused terminal ({POLICY}). Type only "
                "ordinary text, and let the owner exit a session.",
            )
        case ("action", str() as action) if ends_session_action(action):
            return denial(
                f"BLOCKED: `{spelling}` performs the `{action}` action, which quits, closes, or stops its target "
                f"({POLICY}). Perform only actions that leave the session running.",
            )
        case _:
            return None


def orca_verdict(call: Call) -> Denial | None:
    spelling = spell(call)
    arguments = ORCA.bind(call)
    values = arguments.values
    found = next(
        (
            (group, verb)
            for group, verbs in ORCA_ENDINGS.items()
            for verb in verbs
            if group in call.args and verb in call.args
        ),
        None,
    )
    if found is not None:
        return orca_ending(spelling, *found, arguments)
    group = values.get("group", ())
    verb = values.get("verb", ())
    if None in group or None in verb:
        return denial(
            f"BLOCKED: `{spelling}` names its orca group or verb at run time, so the guard cannot tell whether it "
            f"closes a terminal, removes a worktree, or stops a worker ({POLICY}). Spell the group and verb literally.",
        )
    if not arguments.operands_complete and (not group or not verb):
        return denial(
            f"BLOCKED: `{spelling}` hides its orca group or verb behind {hidden_behind(arguments)}, so it may close "
            f"a terminal, remove a worktree, or stop a worker ({POLICY}). Put the group and verb first.",
        )
    match (group[0] if group else None, verb[0] if verb else None):
        case (None, _):
            return None
        case (unknown, _) if unknown not in ORCA_GROUPS:
            return denial(
                f"BLOCKED: `{spelling}` uses an orca command group the guard does not know, so it cannot rule out "
                f"an ending action ({POLICY}). Use a known group, or ask the owner to run it."
            )
        case (str() as group_name, str() as verb_name) if verb_name in ORCA_ENDINGS.get(group_name, ()):
            return orca_ending(spelling, group_name, verb_name, arguments)
        case ("terminal", "send"):
            return orca_send_verdict(spelling, arguments)
        case ("computer", str() as input_verb) if input_verb in ORCA_PAYLOADS:
            return orca_input_verdict(spelling, input_verb, arguments)
        case _:
            return None


def launchctl_verdict(call: Call) -> Denial | None:
    spelling = spell(call)
    arguments = LAUNCHCTL.bind(call)
    match arguments.values.get("verb", ()):
        case (None,):
            return denial(
                f"BLOCKED: `{spelling}` names its launchctl verb at run time, so it may stop, unload, or reboot a "
                f"service that hosts sessions ({POLICY}). Spell the verb literally.",
            )
        case (str() as verb,) if verb in LAUNCHCTL_ENDINGS:
            return denial(
                f"BLOCKED: `{spelling}` stops, unloads, restarts, or reboots a launchd service or domain, and the "
                f"Orca, Captain Hook, and login services host every session ({POLICY}). Inspect instead: "
                "`launchctl list`, `launchctl print <target>`.",
            )
        case () if not arguments.operands_complete:
            return denial(
                f"BLOCKED: `{spelling}` hides its launchctl verb behind {hidden_behind(arguments)} ({POLICY}). "
                "Put the verb first.",
            )
        case _:
            return None


def osascript_verdict(call: Call, facts: Facts) -> Denial | None:
    spelling = spell(call)
    arguments = OSASCRIPT.bind(call)
    statements = arguments.values.get("statement", ())
    if None in statements:
        return denial(
            f"BLOCKED: `{spelling}` runs an AppleScript statement built at run time, which may quit Orca or a "
            f"terminal, log out, restart, or sleep the Mac ({POLICY}). Spell the statement literally.",
        )
    scripts = [str(statement) for statement in statements] or (
        [] if arguments.values.get("program") else [call.cmd.raw]
    )
    if any(APPLESCRIPT_ENDING.search(script) for script in scripts):
        return denial(
            f"BLOCKED: `{spelling}` quits an application, logs out, restarts, shuts down, or sleeps the Mac, which "
            f"ends every session on it ({POLICY}).",
        )
    for match in (
        match
        for script in scripts
        for match in re.finditer(r'(?i)\bdo\s+shell\s+script\b\s*(?:"((?:[^"\\]|\\.)*)")?', script)
    ):
        if (carried := match.group(1)) is None:
            return denial(
                f"BLOCKED: `{spelling}` runs a shell command that AppleScript builds at run time (`do shell script` "
                f"without a literal string), so the guard cannot see what runs ({POLICY}). Spell the shell command "
                "literally.",
            )
        if (
            verdict := text_verdict(
                re.sub(r"\\(.)", r"\1", carried), f"the `do shell script` in `{spelling}`", facts, call.cwd
            )
        ) is not None:
            return verdict
    return None


def softwareupdate_verdict(call: Call) -> Denial | None:
    arguments = SOFTWAREUPDATE.bind(call)
    if "mutate" in arguments.values or not arguments.complete:
        return denial(
            f"BLOCKED: `{spell(call)}` installs or stages a system update, which restarts the Mac and ends every "
            f"session on it ({POLICY}). Read-only forms are fine: `softwareupdate -l`, `--history`.",
        )
    return None


def pmset_verdict(call: Call) -> Denial | None:
    spelling = spell(call)
    arguments = PMSET.bind(call)
    rest = arguments.values.get("rest", ())
    match arguments.values.get("verb", ()):
        case (None,):
            return denial(f"BLOCKED: `{spelling}` names its pmset verb at run time ({POLICY}). Spell it literally.")
        case (str() as verb,) if verb in PMSET_ENDINGS:
            return denial(
                f"BLOCKED: `{spelling}` sleeps, halts, or restarts the Mac, which suspends or ends every session "
                f"on it ({POLICY}). Read power state with `pmset -g`.",
            )
        case ("schedule" | "repeat",) if None in rest or any(str(word) in PMSET_SCHEDULES for word in rest):
            return denial(
                f"BLOCKED: `{spelling}` schedules a shutdown, restart, sleep, or power-off, which ends every "
                f"session on the Mac ({POLICY}). Read the schedule with `pmset -g sched`.",
            )
        case () if not arguments.operands_complete:
            return denial(
                f"BLOCKED: `{spelling}` hides its pmset verb behind {hidden_behind(arguments)} ({POLICY}). "
                "Put the verb first.",
            )
        case _:
            return None


def tmux_verdict(call: Call) -> Denial | None:
    spelling = spell(call)
    arguments = TMUX.bind(call)
    match arguments.values.get("verb", ()):
        case (None,):
            return denial(f"BLOCKED: `{spelling}` names its tmux verb at run time ({POLICY}). Spell it literally.")
        case (str() as verb,) if verb in TMUX_ENDINGS:
            return denial(
                f"BLOCKED: `{spelling}` kills a tmux server, session, window, or pane, ending the agent session "
                f"attached there ({POLICY}). Inspect with `tmux ls` or `tmux list-panes`.",
            )
        case () if not arguments.operands_complete:
            return denial(
                f"BLOCKED: `{spelling}` hides its tmux verb behind {hidden_behind(arguments)} ({POLICY}). "
                "Put the verb first.",
            )
        case _:
            return None


def guarded_program_in(args: tuple[str, ...]) -> str | None:
    return next(
        (
            name
            for arg in args
            for token in re.split(r"[\s;|&()<>=`]+", arg)
            if (name := PurePath(QUOTING_CHARS.sub("", token)).name.casefold()) in GUARDED_PROGRAMS
        ),
        None,
    )


def launcher_verdict(call: Call) -> Denial | None:
    if (inner := guarded_program_in(call.args)) is None:
        return None
    return denial(
        f"BLOCKED: `{spell(call)}` runs `{inner}` through `{call.name}`, which the guard cannot see through, so "
        f"the target cannot be verified ({POLICY}). Run `{inner}` directly as its own command.",
    )


def find_exec_verdict(call: Call) -> Denial | None:
    start = next((index for index, arg in enumerate(call.args) if arg in FIND_EXEC), None)
    if start is None or (inner := guarded_program_in(call.args[start + 1 :])) is None:
        return None
    return denial(
        f"BLOCKED: `{spell(call)}` runs `{inner}` once per matched file through `find -exec`, on targets the "
        f"guard cannot see ({POLICY}). Run `{inner}` directly against literal pids you verified.",
    )


def criteria_denial(name: str) -> Denial:
    return denial(
        f"BLOCKED: {name} signals every process matching a name, pattern, port, or open file rather than one "
        "verified pid, including other sessions' terminals, agents, and daemons: `pkill -f never` killed 15 "
        "unrelated Claude sessions on 2026-09-24, and `pkill -x sleep` ended other sessions' waits on 2026-09-30 "
        f"({POLICY}).",
        VERIFIED_KILL,
    )


def trap_verdict(call: Call, facts: Facts) -> Denial | None:
    words = call.command.words[1:]
    action = next((word for word in words if word.value is None or not word.value.startswith("-")), None)
    if action is None:
        return None
    if action.value is None:
        return denial(
            f"BLOCKED: `{spell(call)}` installs a trap action built at run time on a command line that names a "
            f"session-ending program, so the guard cannot see what runs when it fires ({POLICY}). Spell the action "
            "literally.",
        )
    return text_verdict(action.value, f"the trap action in `{spell(call)}`", facts, call.cwd)


def source_verdict(call: Call) -> Denial | None:
    words = call.command.words[1:]
    script = next((word for word in words if word.value is None or not word.value.startswith("-")), None)
    if script is not None and script.value is not None and script.value not in {"-", "/dev/stdin", "/dev/fd/0"}:
        return None
    return denial(
        f"BLOCKED: `{spell(call)}` sources commands the guard cannot see (stdin, a process substitution, or a file "
        f"named at run time) on a command line that names a session-ending program ({POLICY}). Run the commands "
        "directly.",
    )


def shell_verdict(call: Call) -> Denial | None:
    words = call.command.words[1:]
    spelling = spell(call)
    if call.name == "eval":
        if call.substituted or any(word.value is None for word in words):
            return denial(
                f"BLOCKED: `{spelling}` evaluates text built at run time on a command line that names a "
                f"session-ending program, so the guard cannot see what runs ({POLICY}). Run the command directly.",
            )
        if words and call.occurrence.nesting == PAYLOAD_DEPTH_LIMIT:
            return denial(
                f"BLOCKED: `{spelling}` nests shells deeper than the guard can expand, so the innermost command is "
                f"unchecked ({POLICY}). Flatten the nesting.",
            )
        return None
    flagged = next(
        (
            index
            for index, word in enumerate(words)
            if word.value is not None
            and word.value.startswith("-")
            and not word.value.startswith("--")
            and "c" in word.value[1:]
        ),
        None,
    )
    if flagged is None:
        script = next((word for word in words if word.value is None or not word.value.startswith("-")), None)
        reads_stdin = any(
            word.value is not None
            and word.value.startswith("-")
            and not word.value.startswith("--")
            and "s" in word.value[1:]
            for word in words
        )
        if script is not None and script.value is not None and not reads_stdin:
            return None
        return denial(
            f"BLOCKED: `{spelling}` runs a shell on commands the guard cannot see (stdin, a heredoc, a here-string, "
            f"or a script named at run time) on a command line that names a session-ending program ({POLICY}). "
            "Run the commands directly.",
        )
    payload = next(
        (word for word in words[flagged + 1 :] if word.value is None or not word.value.startswith("-")), None
    )
    if payload is None:
        return None
    if payload.value is None:
        return denial(
            f"BLOCKED: `{spelling}` runs a shell payload built at run time on a command line that names a "
            f"session-ending program, so the guard cannot see what runs ({POLICY}). Run the command directly.",
        )
    if call.occurrence.nesting == PAYLOAD_DEPTH_LIMIT:
        return denial(
            f"BLOCKED: `{spelling}` nests shells deeper than the guard can expand, so the innermost command is "
            f"unchecked ({POLICY}). Flatten the nesting.",
        )
    return None


def call_verdict(call: Call, facts: Facts) -> Denial | None:
    if not (words := call.command.words):
        return None
    head = words[0]
    if head.value is None:
        return denial(
            f"BLOCKED: `{spell(call)}` runs a command named at run time (`{head.raw}`) on a command line that "
            f"also names a session-ending program, so the guard cannot tell what runs ({POLICY}). Spell it literally.",
        )
    if head.expandable and re.search(r"[{*?]|\[.+\]", head.raw) is not None:
        return denial(
            f"BLOCKED: `{spell(call)}` runs a command whose name the shell expands at run time (`{head.raw}`) on a "
            f"command line that also names a session-ending program, so the guard cannot tell what runs ({POLICY}). "
            "Spell it literally.",
        )
    match call.name:
        case "kill":
            return kill_verdict(call, facts)
        case "pkill" | "killall" | "killall5" | "skill" | "snice" | "kill-port" as name:
            return criteria_denial(name)
        case "fuser" if any(
            arg == "--kill" or (arg.startswith("-") and not arg.startswith("--") and "k" in arg[1:])
            for arg in call.args
        ):
            return criteria_denial("fuser -k")
        case "npx" | "bunx" | "pnpx" | "pnpm" | "yarn" if "kill-port" in call.args:
            return criteria_denial("kill-port")
        case "shutdown" | "reboot" | "halt" | "poweroff" as name:
            return denial(
                f"BLOCKED: {name} ends every session on the Mac, including Orca, its terminals, and every agent in "
                f"them ({POLICY}). Nothing an agent does needs a reboot; report the need to the owner instead.",
            )
        case "renice":
            return renice_verdict(call, facts)
        case "orca":
            return orca_verdict(call)
        case "launchctl":
            return launchctl_verdict(call)
        case "osascript":
            return osascript_verdict(call, facts)
        case "softwareupdate":
            return softwareupdate_verdict(call)
        case "pmset":
            return pmset_verdict(call)
        case "tmux":
            return tmux_verdict(call)
        case "find":
            return find_exec_verdict(call)
        case "trap":
            return trap_verdict(call, facts)
        case "source" | "" if head.value in {"source", "."}:
            return source_verdict(call)
        case name if name in LAUNCHERS:
            return launcher_verdict(call)
        case name if name in SHELLS or name == "eval":
            return shell_verdict(call)
        case _:
            return None


def text_verdict(text: str, source: str, facts: Facts, cwd: Path | str | None) -> Denial | None:
    line = safe_parse_command_line(text)
    if calls := () if line is None else Cmd(line, raw=text, cwd=cwd).calls():
        return next((verdict for call in calls if (verdict := call_verdict(call, facts)) is not None), None)
    if (named := GUARDED_PROGRAM.search(QUOTING_CHARS.sub("", text))) is None:
        return None
    return denial(
        f"BLOCKED: {source} names `{named.group(1)}` but is not shell the guard can parse (a comment, non-shell "
        f"code, or nesting too deep), so it cannot verify what runs ({POLICY}). Flatten it into plain commands.",
    )


def candidate_texts(evt: BaseHookEvent) -> Iterator[str]:
    if isinstance(evt.input, BashCall):
        yield evt.input.command
    elif evt.tool_name not in {"Workflow", "Skill"}:
        yield from command_texts(evt.input.raw)


def names_a_guarded_program(evt: BaseHookEvent) -> bool:
    return any(names_guarded(text) for text in candidate_texts(evt))


def first_denial(evt: ToolRewriteEvent) -> Denial | None:
    facts = Facts()
    return next(
        (
            verdict
            for text in filter(names_guarded, candidate_texts(evt))
            if (
                verdict := text_verdict(
                    text.encode(errors="replace").decode(), f"this `{evt.tool_name}` payload", facts, evt.cwd
                )
            )
            is not None
        ),
        None,
    )


@on(
    Event.PreToolUse | Event.PermissionRequest,
    only_if=[LambdaCondition(names_a_guarded_program)],
    respect_gitignore=False,
    skip_planning_agents=False,
    mandatory=True,
    tests={
        guarded(
            command=(
                "pkill -x sleep 2>/dev/null; sleep 0; orca orchestration check --peek --run run_7715a23a5657 --json "
                "2>&1 | head -c 600; echo; echo rc=$?"
            )
        ): Block(pattern="2026-09-30"),
        guarded(command='pkill -f "never" 2>/dev/null; codex-ask --help'): Block(pattern="2026-09-24"),
        guarded(command="pkill node"): Block(),
        guarded(command="sudo pkill node"): Block(),
        guarded(command="pkill -9 -f 'vite dev'"): Block(),
        guarded(command="sudo pkill -f server"): Block(),
        guarded(command='pkill -f "ledger.py watch --repo Forge-AI/monorepo --ledger 829f980d"; sleep 1'): Block(),
        guarded(command='kill %1 2>/dev/null; pkill -f "pr-poll.sh Forge-AI/monorepo 28026" 2>/dev/null'): Block(),
        guarded(command="/usr/bin/pkill x"): Block(),
        guarded(command="xargs pkill"): Block(),
        guarded(command="killall claude"): Block(),
        guarded(command="sudo killall Orca"): Block(),
        guarded(command="killall5 -15"): Block(),
        guarded(command="Kill -9 -1"): Block(),
        guarded(command="Killall claude"): Block(),
        guarded(command="ſhutdown -h now"): Block(),
        guarded(command="fuſer -k 3000/tcp"): Block(),
        guarded(command="fuser -k 3000/tcp"): Block(pattern="fuser -k"),
        guarded(command="fuser -ki -TERM /tmp/sock"): Block(),
        guarded(command="npx kill-port 3000"): Block(pattern="kill-port"),
        guarded(command="bunx kill-port 3000"): Block(),
        guarded(command="fuser -v 3000/tcp"): Allow(),
        guarded(command="pgrep -f pr-poll | xargs kill"): Block(pattern="xargs supplies"),
        guarded(command="pgrep x | xargs kill -0"): Block(pattern="`xargs kill -0`"),
        guarded(
            command=(
                "for x in term_726237b2-b45b-4d94-b15c-18ea259790ad term_707d335b-10ad-4501-81fb-e6cb84799caf; do "
                "orca terminal close --terminal $x --json >/dev/null 2>&1 && echo closed $x; done"
            )
        ): Block(pattern="closes terminal `\\$x`"),
        guarded(
            command=(
                "n=0; for x in $(cat /tmp/reap.txt); do orca terminal close --terminal $x --json >/dev/null 2>&1 && "
                "n=$((n+1)); done; echo closed $n; uptime"
            )
        ): Block(pattern="closes terminal"),
        guarded(
            command=(
                'n=0; while read t; do [ -z "$t" ] && continue; orca terminal close --terminal "$t" >/dev/null 2>&1 '
                "&& n=$((n+1)); done < /tmp/close-list.txt"
            )
        ): Block(pattern="closes terminal"),
        guarded(
            command=(
                'orca terminal list --worktree path:$M/$w --json 2>&1 | python3 -c "import json,sys\nfor t in '
                "json.load(sys.stdin)['result']['terminals']: print(t['handle'])\" | while read t; do orca terminal "
                "close --terminal $t 2>&1 | tail -1; done"
            )
        ): Block(pattern="closes terminal"),
        guarded(command="orca terminal close"): Block(pattern="closes the current terminal"),
        guarded(command="orca terminal close --tab"): Block(pattern="the current tab's terminal"),
        guarded(command="orca terminal close --worktree active --all"): Block(
            pattern="every terminal in worktree `active`"
        ),
        guarded(command="orca --json terminal close --terminal t"): Block(),
        guarded(command="orca terminal --json close --terminal=t"): Block(),
        guarded(command="xargs -n1 orca terminal close --terminal"): Block(),
        guarded(command="orca terminal stop --worktree x"): Block(pattern="stops the active terminal in worktree"),
        guarded(command="orca worktree rm --worktree path:/x"): Block(pattern="removes worktree `path:/x`"),
        guarded(command="orca orchestration worker-stop --dispatch ctx_0dd3cecc2f99"): Block(
            pattern="worker-stop ends the worker or run `ctx_0dd3cecc2f99`"
        ),
        guarded(command="orca orchestration worker-release --dispatch ctx_0dd3cecc2f99"): Block(),
        guarded(command="orca terminal send --terminal t --interrupt"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text exit --enter"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text /exit --enter"): Block(),
        guarded(command="orca terminal send --terminal t --text /quit --enter"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text logout --enter"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text ^C"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text ^D"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text C-c"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text '\\x03'"): Block(pattern="interrupts or exits"),
        guarded(command='orca terminal send --terminal t --text "$MSG" --enter'): Block(pattern="built at run time"),
        guarded(command='orca terminal send --terminal t --text "$(cat /tmp/msg.txt)" --enter'): Block(),
        guarded(command="orca terminal send --terminal t --text hi --enter --timeout-ms 3000"): Block(
            pattern="passes `--timeout-ms`"
        ),
        guarded(command='orca terminal send "$X" --text hi --enter'): Block(pattern="may read as `--interrupt`"),
        guarded(command="orca terminal send --terminal $t --text hi --enter"): Block(pattern="may split"),
        guarded(command="orca terminal send --terminal=$t --text hi --enter"): Block(),
        guarded(command='orca terminal send --terminal="$t" --text hi --enter'): Allow(),
        guarded(
            command='for t in term_a term_b; do orca terminal send --terminal "$t" --text "status?" --enter; done'
        ): Allow(),
        guarded(command='orca terminal send --terminal "$ORCA_TERMINAL_HANDLE" --text "note to self" --enter'): Allow(),
        guarded(command='orca terminal send --worktree "$w" --text hi --enter'): Allow(),
        guarded(command="orca computer hotkey --app Orca --key CmdOrCtrl+Q"): Block(
            pattern="quits, closes, interrupts, or exits the focused session"
        ),
        guarded(command="orca computer hotkey --app Orca --key Cmd+Shift+W"): Block(pattern=r"presses `Cmd\+Shift\+W`"),
        guarded(command="orca computer hotkey --app Orca --key Command+Option+Escape"): Block(),
        guarded(command="orca computer press-key --app Orca --key Ctrl+C"): Block(pattern=r"presses `Ctrl\+C`"),
        guarded(command='orca computer hotkey --app Orca --key "$K"'): Block(pattern="names its key at run time"),
        guarded(command="orca computer hotkey --app Orca --window-index 0 --key Control+D"): Block(),
        guarded(command="orca computer hotkey --app Orca --bogus 1 --key CmdOrCtrl+N"): Block(
            pattern="hides its key behind `--bogus`"
        ),
        guarded(command="orca computer type-text --app Orca --text exit"): Block(pattern="exits the focused terminal"),
        guarded(command="orca computer paste-text --app Orca --text ^C"): Block(pattern="exits the focused terminal"),
        guarded(command="orca computer set-value --app Orca --element-index 3 --value /quit"): Block(),
        guarded(command="orca computer type-text --app Orca --text-stdin < /tmp/payload"): Block(
            pattern=r"names its text at run time \(`--text-stdin`\)"
        ),
        guarded(command='orca computer type-text --app Orca --text "$MSG"'): Block(
            pattern="names its text at run time"
        ),
        guarded(command="orca computer perform-secondary-action --app Orca --element-index 3 --action Quit"): Block(
            pattern="performs the `Quit` action"
        ),
        guarded(
            command="orca computer perform-secondary-action --app Orca --element-index 3 --action AXPress"
        ): Allow(),
        guarded(command="orca computer hotkey --app Orca --key CmdOrCtrl+N"): Allow(),
        guarded(command="orca computer hotkey --app Orca --key Cmd+Shift+N --restore-window"): Allow(),
        guarded(command="orca computer press-key --app Orca --key Escape"): Allow(),
        guarded(command="orca computer press-key --app Orca --key Return"): Allow(),
        guarded(command="orca computer click --app Orca --element-index 12"): Allow(),
        guarded(command='orca computer click --app "$APP" --element-index 3'): Allow(),
        guarded(command='orca computer type-text --app Orca --text "capt-session-guard"'): Allow(),
        guarded(command="orca computer set-value --app Orca --element-index 3 --value feature/x"): Allow(),
        guarded(command="orca computer list-apps"): Allow(),
        guarded(command="orca computer get-app-state --app Orca --json"): Allow(),
        guarded(command="orca computer click --app Terminal --element-index 1"): Allow(),
        guarded(command="orca $GROUP close --terminal t"): Block(pattern="at run time"),
        guarded(command="orca --bogus terminal close"): Block(),
        guarded(command="orca --bogus status"): Block(pattern="hides its orca group or verb behind `--bogus`"),
        guarded(command="orca nuke everything"): Block(pattern="does not know"),
        guarded(command='orca terminal send --terminal t --text "ship it" --enter'): Allow(),
        guarded(command="orca terminal list --json"): Allow(),
        guarded(command="orca terminal show --terminal t"): Allow(),
        guarded(command="orca terminal read --terminal t --screen"): Allow(),
        guarded(command="orca status"): Allow(),
        guarded(command="orca --json status"): Allow(),
        guarded(command="orca orchestration check --peek --run r --json"): Allow(),
        guarded(command="orca worktree list --json"): Allow(),
        guarded(command="kill -9 -123"): Block(pattern="negative process group 123"),
        guarded(command="kill -- -123"): Block(pattern="negative process group 123"),
        guarded(command="kill -TERM -14575"): Block(),
        guarded(command="kill -123"): Block(pattern="negative process group 123"),
        guarded(command="kill 0"): Block(pattern="whole process group"),
        guarded(command="kill -9 -1"): Block(pattern="broadcasts to every process you own"),
        guarded(command="kill $pid"): Block(pattern=r"`\$pid` is not a literal"),
        guarded(command="kill $!"): Block(),
        guarded(command="kill $$"): Block(),
        guarded(command="kill %1"): Block(pattern="job spec"),
        guarded(command="kill $(pgrep x)"): Block(pattern=r"`kill \$\(pgrep x\)` cannot be checked"),
        guarded(command="kill -9 $(pgrep -f server) 2>/dev/null"): Block(pattern="command substitution"),
        guarded(command="kill 12*"): Block(),
        guarded(command="kill 0123"): Block(),
        guarded(command="kill +5"): Block(),
        guarded(command="kill --signal=TERM 123"): Block(pattern="passes `--signal=TERM`"),
        guarded(command="kill -sTERM 123"): Block(pattern="passes `-sTERM`"),
        guarded(command="kill 14575 -l"): Block(pattern="passes `-l`"),
        guarded(command="kill 14575 -s 0"): Block(pattern="passes `-s`"),
        guarded(command="kill 14575 -n 0"): Block(),
        guarded(command="kill -9 14575 -L"): Block(),
        guarded(command="sudo kill 1743 -l"): Block(),
        guarded(command="kill 1743"): Block(pattern="the Orca PTY daemon"),
        guarded(command="kill 1445"): Block(pattern="the Orca app"),
        guarded(command="kill -9 14575"): Block(pattern="an agent session"),
        guarded(command="kill 14545"): Block(pattern="a terminal host"),
        guarded(command="kill 14550"): Block(pattern="an ancestor of a protected process"),
        guarded(command="kill 900"): Block(pattern="the Captain Hook host"),
        guarded(command="kill 99999"): Block(pattern="not in the current process table"),
        guarded(command="sudo kill -9 14575"): Block(pattern="an agent session"),
        guarded(command="env kill 14575"): Block(pattern="an agent session"),
        guarded(command="nohup kill 14575"): Block(pattern="an agent session"),
        guarded(command="timeout 5 kill 14575"): Block(pattern="an agent session"),
        guarded(command="sh -c 'kill 14575'"): Block(pattern="an agent session"),
        guarded(command='bash -c "$CMD"; kill -l'): Block(pattern="shell payload built at run time"),
        guarded(command="p=pkill; $p sleep"): Block(pattern="named at run time"),
        guarded(command='eval "$(echo pkill sleep)"'): Block(pattern="evaluates text built at run time"),
        guarded(command=nested(3, "pkill -x sleep")): Block(pattern="2026-09-30"),
        guarded(command=nested(4, "pkill -x sleep")): Block(pattern="nests shells deeper"),
        guarded(command=nested(3, "eval 'pkill -f claude'")): Block(pattern="nests shells deeper"),
        guarded(command=nested(4, "kill -9 14575", wrapper="eval")): Block(pattern="nests shells deeper"),
        guarded(command="echo pkill -x sleep | sh"): Block(pattern="commands the guard cannot see"),
        guarded(command="bash <<< 'pkill -x sleep'"): Block(pattern="commands the guard cannot see"),
        guarded(command="sh <<'EOF'\npkill -x sleep\nEOF"): Block(pattern="commands the guard cannot see"),
        guarded(command="printf 'kill 14575' | zsh"): Block(),
        guarded(command="bash -s <<< 'kill 14575'"): Block(),
        guarded(command='bash "$SCRIPT"; kill -l'): Block(pattern="commands the guard cannot see"),
        guarded(command="source <(echo pkill -x sleep)"): Block(pattern="sources commands the guard cannot see"),
        guarded(command="echo kill 14575 | . /dev/stdin"): Block(pattern="sources commands"),
        guarded(command="bash ./run-tests.sh; kill -l"): Allow(),
        guarded(command="source ./env.sh; kill -l"): Allow(),
        guarded(command="{kill,14575}"): Block(pattern="expands at run time"),
        guarded(command="{pkill,-x,sleep}"): Block(),
        guarded(command="kill{,} 14575"): Block(),
        guarded(command="/bin/{kill,} 14575"): Block(),
        guarded(command="[k]ill 14575"): Block(),
        guarded(command="~/bin/kill 14575"): Block(pattern="an agent session"),
        guarded(command="trap 'pkill -f claude' EXIT; true"): Block(pattern="2026-09-24"),
        guarded(command='trap "kill -9 14575" EXIT'): Block(pattern="an agent session"),
        guarded(command='trap "$ACT" EXIT; kill -l'): Block(pattern="trap action built at run time"),
        guarded(command="trap 'rm -f /tmp/lock' EXIT; kill -l"): Allow(),
        guarded(command="caffeinate kill 14575"): Block(pattern="cannot see through"),
        guarded(command="setsid kill 14575"): Block(),
        guarded(command="builtin kill 14575"): Block(),
        guarded(command="su root -c 'kill 14575'"): Block(),
        guarded(command="caffeinate sh -c 'x;kill 14575'"): Block(pattern="runs `kill` through `caffeinate`"),
        guarded(command="watch 'true;kill 14575'"): Block(),
        guarded(command='watch -n1 "pgrep x|xargs kill"'): Block(),
        guarded(command="caffeinate /bin/kill 14575"): Block(),
        guarded(command="caffeinate PKILL -f claude"): Block(pattern="runs `pkill`"),
        guarded(command="watch -n1 PKILL -x sleep"): Block(),
        guarded(command="setsid SHUTDOWN -h now"): Block(),
        guarded(command="find /tmp -name '*.pid' -exec kill {} +"): Block(pattern="find -exec"),
        guarded(command="find /tmp -name '*.pid' -exec KILL {} +"): Block(pattern="find -exec"),
        guarded(command="find . -exec sh -c 'kill $1' _ {} \\;"): Block(),
        guarded(command="stdbuf -oL tail -f /tmp/orca.log"): Allow(),
        guarded(command="caffeinate -i make kill-target"): Allow(),
        guarded(command="caffeinate -i ./scripts/reboot-check.sh"): Allow(),
        guarded(command="arch -arm64 make test-kill"): Allow(),
        guarded(command="caffeinate -i make -C /Users/yasyf/.orca/workspaces/captain-hook/x test"): Allow(),
        guarded(command="find . -name '*kill*' -exec cat {} +"): Allow(),
        guarded(command="find ./orca -name x -exec cat {} +"): Allow(),
        guarded(command="/bin/kill 14575"): Block(pattern="an agent session"),
        guarded(command="command kill 14575"): Block(pattern="an agent session"),
        guarded(command="kill -STOP 14575"): Block(),
        guarded(command="kill -s KILL 14575"): Block(),
        guarded(command="kill -0 -9 14575"): Block(),
        guarded(command="kill -0 $pid"): Block(pattern="probe could expand into a real signal"),
        guarded(command='kill -0 "$a" 14575'): Block(pattern="probe could expand"),
        guarded(command='kill -0 "$a" "$b" 14575'): Block(),
        guarded(command='kill -0 "$(cat /tmp/server.pid)"'): Block(pattern="command substitution"),
        guarded(command="renice -n 5 -p 14575"): Block(pattern="an agent session"),
        guarded(command="renice 5 -u yasyf"): Block(pattern="every process of a user"),
        guarded(command="renice -n 5 -g 14575"): Block(),
        guarded(command="renice 5 $pid"): Block(pattern="`renice -n <priority> -p <literal pid>`"),
        guarded(command="renice -n 5 -p 99999"): Block(pattern="would reach an unrelated process"),
        guarded(command="kill -0 14575"): Allow(),
        guarded(command='kill -0 "$worker" 2>/dev/null || break'): Allow(),
        guarded(command="kill -s 0 14575"): Allow(),
        guarded(command="kill -l"): Allow(),
        guarded(command="kill -l 9"): Allow(),
        guarded(command="kill"): Allow(),
        guarded(command="command -v kill"): Allow(),
        guarded(command="command -v renice"): Allow(),
        guarded(command="pgrep -f claude"): Allow(),
        guarded(command="ps -p 1743"): Allow(),
        guarded(command="echo pkill -f never"): Allow(),
        guarded(command="caffeinate -i make test"): Allow(),
        guarded(command="find . -name '*.pyc' -delete"): Allow(),
        guarded(command="renice 5"): Allow(),
        guarded(command="reboot"): Block(pattern="ends every session on the Mac"),
        guarded(command="sudo reboot"): Block(),
        guarded(command="sudo shutdown -r now"): Block(),
        guarded(command="halt"): Block(),
        guarded(command="poweroff"): Block(),
        guarded(command="launchctl reboot system"): Block(pattern="launchctl reboot"),
        guarded(command="launchctl bootout gui/501/com.yasyf.orca-serve"): Block(
            pattern="`launchctl bootout gui/501/com.yasyf.orca-serve`"
        ),
        guarded(command="launchctl kickstart -k system/com.yasyf.captain-hook.host.v1"): Block(),
        guarded(command="launchctl $verb gui/501"): Block(pattern="at run time"),
        guarded(command="launchctl -q bootout gui/501/com.yasyf.orca-serve"): Block(
            pattern="hides its launchctl verb behind `-q`"
        ),
        guarded(command="osascript -e 'tell application \"Orca\" to quit'"): Block(pattern="quits an application"),
        guarded(command="osascript -e 'tell application \"System Events\" to restart'"): Block(),
        guarded(
            command='osascript -e \'tell application "System Events" to keystroke "q" using command down\''
        ): Block(),
        guarded(command='osascript -e "$S"'): Block(pattern="built at run time"),
        guarded(command="osascript <<'EOF'\ntell application \"Orca\" to quit\nEOF"): Block(),
        guarded(command="osascript -e 'do shell script \"pkill -f claude\"'"): Block(pattern="2026-09-24"),
        guarded(command="osascript <<'EOF'\ndo shell script \"kill -9 14575\"\nEOF"): Block(pattern="an agent session"),
        guarded(command="osascript -e 'do shell script theCommand'"): Block(pattern="AppleScript builds at run time"),
        guarded(command="osascript -e 'do shell script \"ls -la\"'"): Allow(),
        guarded(command="softwareupdate -i -a -R"): Block(pattern="installs or stages"),
        guarded(command="softwareupdate --install --all --restart"): Block(),
        guarded(command="softwareupdate --force --install --all"): Block(pattern="installs or stages"),
        guarded(command="pmset sleepnow"): Block(pattern="pmset sleepnow"),
        guarded(command="pmset schedule shutdown '09/30/26 23:00:00'"): Block(),
        guarded(command="pmset repeat shutdown MTWRFSU 23:00:00"): Block(pattern="schedules a shutdown"),
        guarded(command="pmset -z sleepnow"): Block(pattern="hides its pmset verb behind `-z`"),
        guarded(command="pmset repeat cancel"): Allow(),
        guarded(command="tmux kill-server"): Block(pattern="tmux kill-server"),
        guarded(command="tmux -L work kill-session -t main"): Block(pattern="`tmux -L work kill-session -t main`"),
        guarded(command="tmux -N kill-server"): Block(pattern="hides its tmux verb behind `-N`"),
        guarded(command="launchctl list"): Allow(),
        guarded(command="launchctl print gui/501"): Allow(),
        guarded(command="osascript -e 'display notification \"done\"'"): Allow(),
        guarded(command="softwareupdate -l"): Allow(),
        guarded(command="softwareupdate --history"): Allow(),
        guarded(command="pmset -g"): Allow(),
        guarded(command="pmset -g sched"): Allow(),
        guarded(command="tmux ls"): Allow(),
        guarded(command="tmux send-keys -t main ls Enter"): Allow(),
        guarded(command="git status"): Allow(),
        guarded(tool="mcp__runner__exec", tool_input={"cmd": "pkill -x sleep"}): Block(),
        guarded(tool="mcp__runner__exec", tool_input={"command": ["kill", "-9", "14575"]}): Block(
            pattern="an agent session"
        ),
        guarded(tool="mcp__runner__exec", tool_input={"command": ["bash", "-lc", "kill -9 14575"]}): Block(),
        guarded(tool="mcp__runner__exec", tool_input={"command": "kill", "args": ["-9", "14575"]}): Block(
            pattern="an agent session"
        ),
        guarded(
            tool="mcp__runner__exec", tool_input={"command": "orca", "args": ["terminal", "close", "--terminal", "t"]}
        ): Block(pattern="closes terminal `t`"),
        guarded(tool="mcp__runner__exec", tool_input={"argv": ["kill", 14575]}): Block(pattern="an agent session"),
        guarded(tool="mcp__runner__exec", tool_input={"command": ["echo", "hi", ";", "pkill", "-x", "sleep"]}): Block(
            pattern="2026-09-30"
        ),
        guarded(tool="mcp__runner__exec", tool_input={"command": "git", "args": ["status"]}): Allow(),
        guarded(tool="mcp__x__call", tool_input={"opts": {"cmd": "orca terminal close --terminal term_x"}}): Block(
            pattern="closes terminal `term_x`"
        ),
        guarded(tool="mcp__srv__Bash", tool_input={"command": "reboot"}): Block(),
        guarded(
            tool="Monitor", tool_input={"command": "pkill -f poll", "description": "x", "timeout_ms": 1000}
        ): Block(),
        guarded(tool="mcp__x__exec", tool_input={"command": "(" * 2000 + "kill 1" + ")" * 2000}): Block(
            pattern="not shell the guard can parse"
        ),
        guarded(command="# kill 14575 later"): Block(pattern="this `Bash` payload names `kill`"),
        guarded(command="KILL 14575"): Block(pattern="an agent session"),
        guarded(command="p\\kill node"): Block(),
        guarded(command="'orca' terminal close"): Block(),
        guarded(tool="mcp__x__exec", tool_input={"command": "kill 14575 \udc80"}): Block(),
        guarded(tool="mcp__x__call", tool_input={"subject": "pkill", "mode": "x"}): Allow(),
        guarded(tool="SendMessage", tool_input={"to": "a", "message": "never pkill by name"}): Allow(),
        guarded(tool="mcp__runner__exec", tool_input={"cmd": "pgrep -f claude"}): Allow(),
        guarded(
            tool="Workflow",
            tool_input={
                "script": (
                    "const results = await parallel(files.map((f) => () => agent({prompt: `find bugs in ${f}`})));\n"
                    "if (results.ok) { return results; }"
                )
            },
        ): Allow(),
        guarded(tool="Skill", tool_input={"skill": "code-review", "args": "don't kill the dev server"}): Allow(),
        guarded(tool="mcp__jupyter__run", tool_input={"code": "import os\nos.kill(4242, 0)"}): Block(
            pattern="this `mcp__jupyter__run` payload names `kill`"
        ),
        guarded(tool="mcp__jupyter__run", tool_input={"code": "print('watch this ('"}): Allow(),
    },
)
def guard_sessions(evt: ToolRewriteEvent) -> HookResult | None:
    try:
        verdict = first_denial(evt)
    except Exception as exc:  # run_handler turns a raising handler into allow, so a guarded command denies instead
        return evt.block(
            f"BLOCKED: the session guard failed while verifying this command ({type(exc).__name__}: {exc}), and it "
            f"names a session-ending program, so it stays denied ({POLICY}). {OWNER_RUNS_IT}"
        )
    return None if verdict is None else evt.block(verdict.message)
