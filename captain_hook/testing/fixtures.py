from __future__ import annotations

from itertools import count
from typing import Any

from cc_transcript import synthetic

TOOL_USE_IDS = count()


def blocks_of(content: tuple[str | dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [synthetic.text_block(c) if isinstance(c, str) else c for c in content]


class T:
    """Namespaced transcript-fixture builders for ``Input(transcript=[...])`` in inline tests.

    Every builder returns a plain dict — a bare transcript line or a content block —
    that :class:`~captain_hook.testing.types.TranscriptFixture` accepts as-is, so
    ``T.*`` builders and hand-written raw dicts mix freely in one ``transcript`` list.
    Line builders (:meth:`user`, :meth:`assistant`) emit the envelope-free shape the
    fixture loader completes; block builders delegate to ``cc_transcript.synthetic`` so
    the shapes cannot drift from the parser.

    Example:
        >>> from captain_hook.testing.types import Input
        >>> Input(transcript=[
        ...     T.user("Re-enter plan mode, don't do any more work."),
        ...     T.assistant(T.tool("EnterPlanMode")),
        ...     *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest"),
        ... ])
        Input(transcript=...)
    """

    @staticmethod
    def user(*content: str | dict[str, Any], **meta: Any) -> dict[str, Any]:
        """A ``user`` transcript line whose message carries ``content``.

        Args:
            content: Text strings (wrapped as ``text`` blocks) or raw content-block dicts.
            meta: Envelope fields merged onto the line, e.g. ``isMeta=True`` for a meta turn.

        Example:
            >>> T.user("ship it")
            {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'ship it'}]}}
        """
        return {"type": "user", "message": {"content": blocks_of(content)}} | meta

    @staticmethod
    def assistant(*content: str | dict[str, Any], **meta: Any) -> dict[str, Any]:
        """An ``assistant`` transcript line whose message carries ``content``.

        Args:
            content: Text strings (wrapped as ``text`` blocks) or raw content-block dicts,
                typically a :meth:`tool` call block.
            meta: Envelope fields merged onto the line.

        Example:
            >>> T.assistant("planning the change")
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'planning the change'}]}}
        """
        return {"type": "assistant", "message": {"content": blocks_of(content)}} | meta

    @staticmethod
    def tool(name: str, /, *, id: str | None = None, **input: Any) -> dict[str, Any]:
        """A ``tool_use`` content block invoking ``name``; keyword arguments become its input.

        Args:
            name: The tool name, e.g. ``"Bash"`` or ``"EnterPlanMode"``.
            id: The ``tool_use`` id; a fresh ``tu-N`` id is minted when omitted. Keep
                explicit ids outside the ``tu-<int>`` namespace auto-minting uses.
            input: The tool input fields, e.g. ``command="uv run pytest"``.

        Example:
            >>> T.tool("Bash", command="uv run pytest")
            {'type': 'tool_use', 'id': ..., 'name': 'Bash', 'input': {'command': 'uv run pytest'}}
        """
        return synthetic.tool_use(id or f"tu-{next(TOOL_USE_IDS)}", name, input)

    @staticmethod
    def result(
        content: str = "ok", *, of: dict[str, Any] | str | None = None, is_error: bool = False
    ) -> dict[str, Any]:
        """A ``tool_result`` content block; ``of`` correlates it to the call it answers.

        Args:
            content: The result text surfaced to the tool caller.
            of: The call this result answers — a :meth:`tool` block (its id is read),
                a raw ``tool_use`` id string, or ``None`` to mint a fresh unpaired id.
            is_error: Marks a failed call, lifting to a failure in the parsed session.

        Example:
            >>> call = T.tool("Bash", command="uv run pytest")
            >>> T.result("ModuleNotFoundError", of=call, is_error=True)
            {'type': 'tool_result', 'tool_use_id': ..., 'content': 'ModuleNotFoundError', 'is_error': True}
        """
        match of:
            case {"id": str() as tool_use_id}:
                pass
            case str() as tool_use_id:
                pass
            case None:
                tool_use_id = f"tu-{next(TOOL_USE_IDS)}"
            case _:
                raise TypeError(f"of must be a tool_use block, an id string, or None, got {of!r}")
        return synthetic.tool_result(tool_use_id, content, is_error=is_error)

    @staticmethod
    def thinking(text: str) -> dict[str, Any]:
        """A ``thinking`` content block for :meth:`assistant`.

        Example:
            >>> T.assistant(T.thinking("The user asked for a rename only."))
            {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': ...}]}}
        """
        return synthetic.thinking_block(text)

    @staticmethod
    def tool_turn(name: str, /, *, result: str = "ok", is_error: bool = False, **input: Any) -> list[dict[str, Any]]:
        """A paired assistant tool-call line and user tool-result line with matched ids.

        Splat the returned pair into a ``transcript`` list to model one full tool round-trip.

        Args:
            name: The tool name, e.g. ``"Bash"``.
            result: The tool-result content joined back to the call.
            is_error: Marks the call as failed.
            input: The tool input fields, e.g. ``command="uv run pytest"``.

        Example:
            >>> T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest")
            [{'type': 'assistant', ...}, {'type': 'user', ...}]
        """
        return [T.assistant(tu := T.tool(name, **input)), T.user(T.result(result, of=tu, is_error=is_error))]
