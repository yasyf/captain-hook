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

CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
COMMAND_KEY = re.compile(r"(?i)^(cmd|command|script|shell|exec|args|argv|run|code)$")
SCAN_CAP = 8192


def command_texts(value: object) -> Iterator[str]:
    match value:
        case dict() as mapping:
            for key, val in mapping.items():
                match val:
                    case str() if COMMAND_KEY.match(key):
                        yield val
                    case list() if COMMAND_KEY.match(key):
                        if joined := " ".join(item for item in val if isinstance(item, str)):
                            yield joined
                        yield from command_texts(val)
                    case dict() | list():
                        yield from command_texts(val)
        case list() as items:
            for item in items:
                if isinstance(item, dict | list):
                    yield from command_texts(item)


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

    Recurses the full input structure (dicts anywhere, dicts inside lists included) but
    scans only values whose key names a command carrier (``cmd``/``command``/``script``/
    ``shell``/``exec``/``args``/``argv``/``run``/``code``) — a string, or a list whose
    string items join into one argv-style line. Each candidate is scanned individually
    (never concatenated across keys) and capped at 8KB: a courtesy guard, not a boundary.
    """

    pattern: str

    def __post_init__(self) -> None:
        re.compile(self.pattern)

    def check(self, evt: BaseHookEvent) -> bool:
        return any(re.search(self.pattern, text[:SCAN_CAP]) for text in command_texts(evt.input.raw))


class DangerousMcpTool(CustomCondition):
    """Matches MCP tools whose tool segment carries a destructive verb token.

    The segment splits on non-alphanumeric runs and camelCase boundaries, then tokens are
    matched whole against the verb set: ``DELETE_EVERYTHING`` and ``eraseBucket`` match,
    ``set_dropdown`` and ``transform`` don't. Token matching over substring matching is a
    deliberate tradeoff — separator-free names (``droptable``) pass, and camel compounds
    that shatter into a verb (``setDropDown`` → ``drop``) fail closed to a prompt.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        match (evt.tool_name or "").split("__", 2):
            case ["mcp", _, tool]:
                return not DANGEROUS_MCP_VERBS.isdisjoint(
                    token.lower() for chunk in NON_ALNUM.split(tool) for token in CAMEL_BOUNDARY.split(chunk) if token
                )
            case _:
                return False
