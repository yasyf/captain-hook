from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from functools import cached_property, partial, reduce
from itertools import product
from math import prod
from pathlib import PurePath
from typing import TYPE_CHECKING

from cc_transcript.tools import BashCall

from captain_hook import Event, Input, LambdaCondition, on
from captain_hook.bindings import Resolution, Resolved, Unknown, Unresolved, program_name, references
from captain_hook.cmd import Cmd
from captain_hook.command_schemas import OSASCRIPT
from captain_hook.dispatch import SYNC_DEADLINE_MARGIN_SECONDS, collect_budget
from captain_hook.guard_literal import FOLD_TABLE, GUARDED_WORD, QUOTING_CHARS, names_guarded
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
PROBE_TIMEOUT = 2.0
LAST_SCAN = threading.local()
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
INLINE_OWNER_ANSWER = "0207568"
INLINE_OWNER_TERMINAL = "term_c59a87bf-0000-4000-8000-000000000000"
INLINE_COMMANDS = {
    "ps -A -ww -o": INLINE_TABLE,
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
    "ccn answer show": "{}",
    f"ccn answer show {INLINE_OWNER_ANSWER}": json.dumps(
        {"body": f"Owner: close exactly {INLINE_OWNER_TERMINAL} and term_agent, nothing else."}
    ),
}

guarded = partial(Input, commands=INLINE_COMMANDS)


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


def probe_timeout(reason: str) -> float | Unreadable:
    budget = collect_budget(SYNC_DEADLINE_MARGIN_SECONDS)
    if budget is None:
        return PROBE_TIMEOUT
    if budget < 0.5:
        return Unreadable(f"the caller deadline is too close to {reason}")
    return min(PROBE_TIMEOUT, budget - 0.25)


def probe(argv: tuple[str, ...]) -> str | Unreadable:
    if isinstance(timeout := probe_timeout(f"run `{argv[0]}`"), Unreadable):
        return timeout
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False
        )
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


def answer_names(answer: str, handle: str, cwd: Path | None) -> bool | Unreadable:
    location = () if cwd is None else ("-R", str(cwd))
    shown = probe(("ccn", "answer", "show", answer, "--json", *location))
    if isinstance(shown, Unreadable):
        return shown
    try:
        body = json.loads(shown)["body"]
    except (ValueError, TypeError, KeyError):
        return Unreadable("it has no body")
    return isinstance(body, str) and re.search(rf"(?<![\w-]){re.escape(handle)}(?![\w-])", body) is not None


class Facts:
    @cached_property
    def ownership(self) -> Ownership | Unreadable:
        if isinstance(timeout := probe_timeout("read the process table"), Unreadable):
            return timeout
        table = proc.process_table(timeout=timeout)
        return Unreadable("the process table could not be read") if table is None else Ownership.resolve(table)

    def terminal_tree(self, handle: str) -> tuple[ProcessRow, ...] | Unreadable:
        if isinstance(ownership := self.ownership, Unreadable):
            return ownership
        if isinstance(pid := terminal_pid(handle), Unreadable):
            return pid
        if (root := ownership.table.rows.get(pid)) is None:
            return Unreadable(f"the terminal's process {pid} is not in the process table")
        return (root, *ownership.table.descendants(pid))


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
    raw: object
    facts: Facts = field(default_factory=Facts)
    calls: list[Call] = field(default_factory=list)
    respelled: list[Call] = field(default_factory=list)
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
