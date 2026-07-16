from __future__ import annotations

import re
from dataclasses import dataclass

from captain_hook import BaseHookEvent, CustomCondition

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

VERB_TOKEN = re.compile(r"[a-z]+|[A-Z][a-z]*|\d+")


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
    """Matches a regex against every string in the tool input: top-level values plus list items."""

    pattern: str

    def __post_init__(self) -> None:
        re.compile(self.pattern)

    def check(self, evt: BaseHookEvent) -> bool:
        texts = [
            item
            for value in evt.input.raw.values()
            for item in (value if isinstance(value, list) else (value,))
            if isinstance(item, str)
        ]
        return bool(re.search(self.pattern, "\n".join(texts)))


class DangerousMcpTool(CustomCondition):
    """Matches MCP tools whose tool segment carries a destructive verb token (``mcp__ops__deleteEverything``)."""

    def check(self, evt: BaseHookEvent) -> bool:
        match (evt.tool_name or "").split("__", 2):
            case ["mcp", _, tool]:
                return not DANGEROUS_MCP_VERBS.isdisjoint(token.lower() for token in VERB_TOKEN.findall(tool))
            case _:
                return False
