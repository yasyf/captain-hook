from __future__ import annotations

from typing import TYPE_CHECKING, Any

from captain_hook.transcripts import register_transcript

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def build_mcp_server() -> MCPServer:
    """Build the stdio MCP server that exposes ``register_transcript`` as its one tool.

    The ``mcp`` SDK imports lazily so that it stays off every other subcommand's startup path.
    """
    import importlib.metadata

    from mcp.server.mcpserver import MCPServer

    server = MCPServer("capt-hook", version=importlib.metadata.version("capt-hook"))

    @server.tool(name="register_transcript")
    def register_transcript_tool(
        session_id: str,
        provider: str = "codex",
        thread_id: str | None = None,
        path: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Register an external transcript against a Claude Code session so its edits fold into the deep view.

        Exactly one of ``thread_id`` (a codex thread id, resolved to its rollout at dispatch) or
        ``path`` (a direct transcript file path) locates the transcript. Registration is idempotent.
        """
        return register_transcript(
            session_id, provider=provider, thread_id=thread_id, path=path, label=label
        ).model_dump()

    return server
