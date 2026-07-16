from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import BaseHookEvent, CustomCondition

if TYPE_CHECKING:
    from collections.abc import Iterator

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

CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
NON_LETTER = re.compile(r"[^A-Za-z]+")
COMMAND_KEY = re.compile(r"cmd|command|script|shell|exec|args|argv|run|code", re.ASCII | re.IGNORECASE)
SCAN_CAP = 8192
MAX_SCAN_DEPTH = 12


def payload_leaves(items: list[object], depth: int) -> Iterator[object]:
    for item in items:
        match item:
            case list() if depth > 0:
                yield from payload_leaves(item, depth - 1)
            case list():
                pass
            case _:
                yield item


def command_texts(value: object, depth: int = MAX_SCAN_DEPTH) -> Iterator[str]:
    if depth <= 0:
        return
    match value:
        case dict() as mapping:
            for key, val in mapping.items():
                match val:
                    case str() if COMMAND_KEY.fullmatch(key):
                        yield val
                    case list() if COMMAND_KEY.fullmatch(key):
                        leaves = list(payload_leaves(val, depth - 1))
                        if leaves and all(isinstance(leaf, str) for leaf in leaves):
                            yield " ".join(leaves)
                        else:
                            yield from (leaf for leaf in leaves if isinstance(leaf, str))
                        yield from command_texts(val, depth - 1)
                    case dict() | list():
                        yield from command_texts(val, depth - 1)
        case list() as items:
            for item in items:
                if isinstance(item, dict | list):
                    yield from command_texts(item, depth - 1)


class McpTool(CustomCondition):
    """Matches MCP-server tools (``mcp__<server>__<tool>``), which Tool() suffix-matching also accepts."""

    def check(self, evt: BaseHookEvent) -> bool:
        return bool(evt.tool_name) and evt.tool_name.startswith("mcp__")


@dataclass(frozen=True, slots=True)
class NativeTool(CustomCondition):
    """Matches the event's tool name exactly — no alias or MCP-suffix expansion, unlike Tool()."""

    name: str

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.tool_name == self.name


@dataclass(frozen=True, slots=True)
class PayloadText(CustomCondition):
    """Matches a regex against command-keyed strings anywhere in the tool input.

    Recurses the input structure to ``MAX_SCAN_DEPTH`` (deeper is simply not descended)
    but scans only values whose key spells a command carrier exactly, ASCII-only
    (``cmd``/``command``/``script``/``shell``/``exec``/``args``/``argv``/``run``/``code``).
    A carrier string is one candidate; a carrier list flattens to its leaves — all-string
    leaves join into one argv-style line, mixed leaves scan each string individually so
    unrelated items never concatenate into a match. Each candidate is scanned on its own
    and capped at 8KB: a courtesy guard, not a boundary.
    """

    pattern: str

    def __post_init__(self) -> None:
        re.compile(self.pattern)

    def check(self, evt: BaseHookEvent) -> bool:
        return any(re.search(self.pattern, text[:SCAN_CAP]) for text in command_texts(evt.input.raw))


class DangerousMcpTool(CustomCondition):
    """Matches MCP tools whose tool segment carries a destructive verb token.

    The segment splits on non-letter runs (digits separate: ``delete2`` and ``reset2fa``
    match, ``base64_decode`` doesn't) and camelCase boundaries, then tokens are matched
    whole against the verb set: ``DELETE_EVERYTHING`` and ``eraseBucket`` match,
    ``set_dropdown`` and ``transform`` don't. Token matching over substring matching is a
    deliberate tradeoff — separator-free names (``droptable``) pass, and camel compounds
    that shatter into a verb (``setDropDown`` → ``drop``) fail closed to a prompt.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        match (evt.tool_name or "").split("__", 2):
            case ["mcp", _, tool]:
                return not DANGEROUS_MCP_VERBS.isdisjoint(
                    token.lower() for chunk in NON_LETTER.split(tool) for token in CAMEL_BOUNDARY.split(chunk) if token
                )
            case _:
                return False
