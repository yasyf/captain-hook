from __future__ import annotations

from dataclasses import dataclass

from captain_hook import BaseHookEvent, CustomCondition

DANGEROUS_MCP_VERBS = frozenset({"delete", "remove", "destroy", "drop", "purge", "wipe", "publish", "deploy"})


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


class DangerousMcpTool(CustomCondition):
    """Matches MCP tools whose tool segment carries a destructive verb token (``mcp__ops__delete_everything``)."""

    def check(self, evt: BaseHookEvent) -> bool:
        match (evt.tool_name or "").split("__", 2):
            case ["mcp", _, tool]:
                return not DANGEROUS_MCP_VERBS.isdisjoint(tool.lower().split("_"))
            case _:
                return False
