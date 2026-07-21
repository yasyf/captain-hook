from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from cc_transcript.tools import mcp_parts

from captain_hook import BaseHookEvent, CustomCommandLineCondition, CustomCondition
from captain_hook.cmd import Cmd
from captain_hook.util.shell import SHELLS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cc_transcript.command import CommandLine

    from captain_hook.cmd import Call

DANGEROUS_MCP_VERBS = frozenset(
    {
        "delete",
        "remove",
        "destroy",
        "drop",
        "purge",
        "wipe",
        "publish",
        "deploy",
        "erase",
        "truncate",
        "revoke",
        "reset",
        "uninstall",
        "rm",
        "kill",
        "terminate",
    }
)

COMMAND_KEY = re.compile(r"cmd|command|script|shell|exec|args|argv|run|code", re.ASCII | re.IGNORECASE)
MAX_SCAN_DEPTH = 12

DESTRUCTIVE_EXECUTABLES = frozenset({"rm", "dd", "shred", "truncate"})
DOWNLOADERS = frozenset({"curl", "wget"})
FORCE_PUSH_FLAG = re.compile(r"(--?force(-with-lease)?|-f|--delete)(=.*)?")
PAYLOAD_SCAN_LIMIT = 8192


def payload_leaves(items: list[object], depth: int) -> Iterator[object]:
    for item in items:
        match item:
            case list() if depth > 0:
                yield from payload_leaves(item, depth - 1)
            case list():
                pass
            case _:
                yield item


def list_leaf_texts(items: list[object], depth: int) -> Iterator[str]:
    leaves = list(payload_leaves(items, depth))
    if leaves and all(isinstance(leaf, str) for leaf in leaves):
        yield " ".join(leaves)
    else:
        yield from (leaf for leaf in leaves if isinstance(leaf, str))


def command_texts(value: object, depth: int = MAX_SCAN_DEPTH) -> Iterator[str]:
    match value:
        case dict() as mapping:
            for key, val in mapping.items():
                match val:
                    case str() if COMMAND_KEY.fullmatch(key):
                        yield val
                    case list() if COMMAND_KEY.fullmatch(key):
                        yield from list_leaf_texts(val, depth)
                        if depth > 0:
                            yield from command_texts(val, depth - 1)
                    case dict() | list() if depth > 0:
                        yield from command_texts(val, depth - 1)
        case list() as items if depth > 0:
            for item in items:
                if isinstance(item, dict | list):
                    yield from command_texts(item, depth - 1)


def is_dangerous_call(call: Call) -> bool:
    if "sudo" in call.wrappers or call.name == "sudo":
        return True
    if call.name in DESTRUCTIVE_EXECUTABLES or call.name.startswith("mkfs"):
        return True
    if call.name == "git":
        match call.targets.targets[0].value if call.targets else None:
            case "reset" | "clean" | "restore":
                return True
            case "push":
                return any(FORCE_PUSH_FLAG.fullmatch(flag) for flag in call.flags)
    return False


def pipes_into_shell(cmd: Cmd) -> bool:
    return any(
        call.occurrence.next_op == "|" and call.name in DOWNLOADERS and nxt.name in SHELLS
        for call, nxt in pairwise(cmd.calls())
    )


def is_dangerous_cmd(cmd: Cmd) -> bool:
    return any(is_dangerous_call(call) for call in cmd.calls()) or pipes_into_shell(cmd)


class McpTool(CustomCondition):
    """Matches MCP-server tools (``mcp__<server>__<tool>``), which Tool() suffix-matching also accepts."""

    def check(self, evt: BaseHookEvent) -> bool:
        return mcp_parts(evt.tool_name or "") is not None


@dataclass(frozen=True, slots=True)
class NativeTool(CustomCondition):
    """Matches the event's tool name exactly — no alias or MCP-suffix expansion, unlike Tool()."""

    name: str

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.tool_name == self.name


class DangerousCommandLine(CustomCommandLineCondition):
    """Matches a native Bash line that runs a destructive command in command position.

    Parses the line (via ``evt.cmd``) and flags a command whose unwrapped executable is
    destructive (``rm``/``dd``/``shred``/``truncate``/``mkfs*``), is ``sudo``, is a dangerous
    ``git`` subcommand (``reset``/``clean``/``restore``, or ``push`` with a force/delete flag),
    or a downloader piped into a shell. Nested ``sh -c``/``eval`` payloads and command
    substitutions are covered because the parser enumerates them as their own commands. A repo
    or path whose name merely contains ``sudo`` or ``rm`` is an argument token, never in command
    position, so it does not match. A courtesy speed bump, not a security boundary.
    """

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return is_dangerous_cmd(evt.cmd)


class DangerousPayloadCommand(CustomCondition):
    """Matches MCP tool payloads whose command-carrying strings parse to a destructive command.

    Pulls candidate strings from the input via ``command_texts`` (values under command-carrier
    keys at any nesting depth), then parses each and applies the same structural predicate as
    ``DangerousCommandLine``. Each candidate is capped at 8KB before parsing: a courtesy guard,
    not a boundary.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        # Replace lone surrogates (unencodable, would crash the parser) and cap before Cmd.parse,
        # which itself falls open (None) on pathological nesting.
        return any(
            (cmd := Cmd.parse(text[:PAYLOAD_SCAN_LIMIT].encode(errors="replace").decode())) is not None
            and is_dangerous_cmd(cmd)
            for text in command_texts(evt.input.raw)
        )


class DangerousMcpTool(CustomCondition):
    """Matches MCP tools whose tool segment carries a destructive verb token.

    The segment splits on non-letter runs (digits separate: ``delete2`` and ``reset2fa``
    match, ``base64_decode`` doesn't) and camelCase boundaries, then tokens are matched
    whole against the verb set: ``DELETE_EVERYTHING`` and ``eraseBucket`` match,
    ``set_dropdown`` and ``transform`` don't. Token matching over substring matching is a
    deliberate tradeoff — separator-free names (``droptable``) pass, and camel compounds
    that shatter into a verb (``setDropDown`` → ``drop``) fail closed to a prompt.
    Digit-inside-verb spellings (``de2lete``, ``rem0ve``) are likewise out of the threat
    model: an adversarial server can name a destructive tool ``get_info`` — this guards
    against accidental naming, not adversarial naming.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        match mcp_parts(evt.tool_name or ""):
            case (_, tool):
                return not DANGEROUS_MCP_VERBS.isdisjoint(
                    token.lower()
                    for chunk in re.split(r"[^A-Za-z]+", tool)
                    for token in re.split(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", chunk)
                    if token
                )
            case _:
                return False
