from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import subprocess
from copy import copy
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from captain_hook.app import on
from captain_hook.bindings import tokens
from captain_hook.cmd import Cmd
from captain_hook.conditions import any_condition, check_condition
from captain_hook.state import SeenKeys
from captain_hook.types import TOOL_EVENTS, CustomCondition, Event, Tool
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from cc_transcript.command import Word

    from captain_hook.cmd import Call
    from captain_hook.events import BaseHookEvent, PreToolUseEvent
    from captain_hook.types import HookResult, InlineTests, TCondition

CC_SUDO = "cc-sudo"
CARD_COMMAND_ENV = "HOOKS_TAP_CARD_COMMAND"
CARD_SCOPE = "tap-card"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
LAUNCHCTL_RUNNERS = frozenset({"asuser", "bsexec"})
PROMPTED_WRAPPERS = frozenset({"sudo"})
PROBE_SECONDS = 10
IDLE_LIMIT_NS = 300 * 1_000_000_000
UNLOCKED = re.compile(r'"IOConsoleLocked" = No\b')
HID_IDLE = re.compile(r'"HIDIdleTime" = (\d+)')
INEXACT = "A Touch ID tap covers one literal command with no shell, wrapper or substitution around it. Run this form:"
TAP_INCOMPLETE = "Retry the call; a command needing a Touch ID tap runs only once this check completes."


@dataclass(frozen=True, slots=True)
class TapGuarded(CustomCondition):
    """The one narrow match a fail-closed tap hook registers on, so a missed deadline holds only what it guards."""

    valid_events = TOOL_EVENTS
    matches: Callable[[BaseHookEvent], bool]

    def check(self, evt: BaseHookEvent) -> bool:
        return self.matches(evt)


def probe(*argv: str) -> str:
    """The stdout of a read-only tool, empty when the tool is missing or stalls past the bound."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_SECONDS, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cc_sudo_installed() -> bool:
    return bool(probe(CC_SUDO, "--version"))


def at_the_mac() -> bool:
    """Whether a person can answer a Touch ID prompt here: the console is unlocked and took physical input recently.

    The HID idle clock counts keyboard and mouse events on this Mac, so a phone tap, a remote shell,
    and an agent writing to a terminal never read as presence.
    """
    if not UNLOCKED.search(probe("ioreg", "-n", "Root", "-d1")):
        return False
    idle = HID_IDLE.search(probe("ioreg", "-c", "IOHIDSystem", "-d", "4"))
    return idle is not None and int(idle[1]) < IDLE_LIMIT_NS


def queue_card(evt: BaseHookEvent, command: str) -> bool:
    """Ask the owner for a tap through ``HOOKS_TAP_CARD_COMMAND``, once per exact command in a session.

    The configured command receives the request as its last argument and runs as the calling session,
    so a poster that files under the caller's identity needs no name of its own. Returns whether the ask
    stands, so a box with nothing configured or a poster that failed leaves the ask to the agent.
    """
    if not (poster := reqenv.getenv(CARD_COMMAND_ENV)):
        return False
    if command in evt.ctx.s.load(SeenKeys).seen.get(CARD_SCOPE, []):
        return True
    request = f"A lane needs the owner's Touch ID tap and the owner is away from this Mac: {command}"
    try:
        posted = subprocess.run(
            [*shlex.split(poster), request],
            capture_output=True,
            text=True,
            timeout=PROBE_SECONDS,
            check=False,
            env={**reqenv.env_map(), SESSION_ENV: evt.session_id},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if posted.returncode != 0:
        return False
    evt.ctx.s.once(command, scope=CARD_SCOPE)
    return True


def parsed(words: Iterable[Word]) -> Cmd | None:
    return Cmd.parse(" ".join(word.raw for word in words))


def tapped(call: Call) -> Cmd | None:
    """The command ``cc-sudo run -- <argv>`` asks a tap for, or None for any other call."""
    return parsed(call.command.words[3:]) if call.name == CC_SUDO and call.args[:2] == ("run", "--") else None


def launched(call: Call) -> Cmd | None:
    """The command ``launchctl asuser <id> <argv>`` runs, which the parser reads as launchctl's own arguments."""
    is_runner = call.name == "launchctl" and bool(LAUNCHCTL_RUNNERS.intersection(call.args[:1]))
    return parsed(call.command.words[3:]) if is_runner else None


def shadowed(cmd: Cmd) -> bool:
    """Whether the line can change which program the name ``cc-sudo`` runs."""
    words = [token.raw for token in tokens(cmd.raw)]
    return (
        "PATH" in cmd.scope.mentioned | cmd.scope.mutated
        or bool(cmd.calls("alias"))
        or any(pair in ((CC_SUDO, "("), ("function", CC_SUDO)) for pair in pairwise(words))
    )


def wrapped_exactly(line: Cmd, call: Call) -> bool:
    """Whether ``call`` is the real wrapper, on the literal argv the prompt will show, with nothing changing either."""
    return (
        call.command.words[0].value == CC_SUDO
        and not (call.source.env or call.wrappers or call.substituted or call.occurrence.quote_contexts)
        and all(word.value is not None for word in call.command.words[3:])
        and not shadowed(line)
    )


def as_owner(words: Iterable[Word]) -> str:
    """``cc-sudo`` runs as root, so a read of the owner's own store goes back through the owner's GUI session."""
    head, *rest = (word.raw for word in words)
    uid = os.getuid()
    resolved = shutil.which(head) or head
    return " ".join(
        ("/bin/launchctl", "asuser", str(uid), "/usr/bin/sudo", "-H", "-u", pwd.getpwuid(uid).pw_name, resolved, *rest)
    )


def tap_command(
    *,
    only_if: Sequence[TCondition],
    rule: str,
    refusal: str,
    as_user: bool = False,
    skip_if: Sequence[TCondition] = (),
    tests: InlineTests | None = None,
) -> None:
    """Register a Bash guard that gives way to the owner's Touch ID tap on the exact command.

    A command matching ``only_if`` is refused with its ``cc-sudo run -- <command>`` form named, and
    that form passes, so the native prompt shows what runs and one tap covers one command. The
    match reads past ``launchctl asuser`` and through a wrapper's argv. A wrapper around a matching
    command passes only when its argv is literal, runs alone, and the line cannot redirect the name
    ``cc-sudo``. With no ``cc-sudo`` on the box every match gets ``refusal``. Conditions must be
    structural (``CommandMatches``, ``Runs``), since a regex over the raw line also matches the wrapped form.

    Args:
        only_if: The conditions naming the guarded command, ANDed.
        rule: Why a tap is needed; the refusal appends the exact form to run in a fence.
        refusal: The message where no tap can ever be had.
        as_user: Name a form that runs as the session's user, for a command that reads the user's own
            stores, since ``cc-sudo`` runs its argv as root.
        skip_if: Conditions vetoing the guard.
        tests: Inline tests; stub ``commands={"cc-sudo --version": ...}`` to pick the box.

    Example:
        >>> tap_command(only_if=[Runs("op", "read")], rule="Reads need a tap.", refusal="Ask the owner.")
    """

    def fires(evt: BaseHookEvent, cmd: Cmd) -> bool:
        fork = copy(evt)
        fork.__dict__["cmd"] = cmd
        return all(check_condition(condition, fork) for condition in only_if) and not any_condition(skip_if, fork)

    def carries(evt: BaseHookEvent, cmd: Cmd) -> bool:
        return fires(evt, cmd) or any(
            carries(evt, inner) for call in cmd.calls() for inner in (launched(call), tapped(call)) if inner is not None
        )

    def bare(evt: BaseHookEvent) -> bool:
        return fires(evt, evt.cmd) or any(
            carries(evt, inner) for call in evt.cmd.calls() if (inner := launched(call)) is not None
        )

    def runs_alone(evt: BaseHookEvent, cmd: Cmd) -> bool:
        calls = cmd.calls()
        if len(calls) != 1 or calls[0].source.env or set(calls[0].wrappers) - PROMPTED_WRAPPERS:
            return False
        inner = launched(calls[0])
        return fires(evt, cmd) if inner is None else runs_alone(evt, inner)

    def named(evt: BaseHookEvent, cmd: Cmd) -> Call | None:
        for call in cmd.calls():
            inner = launched(call) if call.name != CC_SUDO else tapped(call)
            found = named(evt, inner) if inner is not None else call if single_read(evt, call) else None
            if found is not None:
                return found
        return None

    def single_read(evt: BaseHookEvent, call: Call) -> bool:
        alone = parsed(call.command.words)
        return alone is not None and len(alone.calls()) == 1 and fires(evt, alone)

    def form(evt: BaseHookEvent, cmd: Cmd) -> str:
        if (call := named(evt, cmd)) is None:
            return f"{CC_SUDO} run -- <the exact command>"
        words = call.command.words
        return f"{CC_SUDO} run -- {as_owner(words) if as_user else ' '.join(word.raw for word in words)}"

    def wrapped(evt: BaseHookEvent) -> list[tuple[Call, Cmd]]:
        return [
            (call, payload)
            for call in evt.cmd.calls()
            if (payload := tapped(call)) is not None and carries(evt, payload)
        ]

    @on(
        Event.PreToolUse,
        only_if=[Tool("Bash"), TapGuarded(lambda evt: bare(evt) or bool(wrapped(evt)))],
        on_incomplete=TAP_INCOMPLETE,
        tests=tests,
    )
    def tap_guard(evt: PreToolUseEvent) -> HookResult | None:
        if not cc_sudo_installed():
            return evt.block(refusal)
        if bare(evt):
            return evt.block(f"{rule} Run this form:\n```\n{form(evt, evt.cmd)}\n```")
        for call, payload in wrapped(evt):
            if not (wrapped_exactly(evt.cmd, call) and runs_alone(evt, payload)):
                return evt.block(f"{INEXACT}\n```\n{form(evt, payload)}\n```")
        return None
