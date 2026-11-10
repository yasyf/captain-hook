from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cc_transcript.query import Session

if TYPE_CHECKING:
    from captain_hook.signals.nlp import Clause

INJECTED_BLOCK = re.compile(
    r"<(task-notification|channel|system-reminder|teammate-message|agent-message|cross-session-message|pasted_content)"
    r"\b[^>]*>.*?</\1\b[^>]*>",
    re.DOTALL,
)


def typed_words(prompt: str) -> str:
    """The words a person typed in *prompt*, with every block the harness or another agent injected cut out.

    A task notification, channel message, system reminder, agent or cross-session message, or pasted
    content opens or joins a turn's prompt without anyone typing it, so it never directs the agent.

    Example:
        >>> typed_words("<task-notification><result>I plan the release</result></task-notification>").strip()
        ''
    """
    return INJECTED_BLOCK.sub(" ", prompt)


class Turn(Session):
    """The current turn as a one-turn :class:`~cc_transcript.query.Session` view.

    What ``evt.ctx.turn`` returns: the full windowed transcript surface
    (``user_text``, ``has_tool``, ``tool_calls``, …) plus prompt matching, so
    hooks can test the turn's opening prompt without importing the NLP machinery.
    """

    __slots__ = ()

    def matches(self, *patterns: str | Clause) -> bool:
        """Whether the words a person typed in the turn's opening prompt match any pattern.

        A string pattern is a case-insensitive regex; a
        :class:`~captain_hook.Clause` runs the dependency-clause scan.
        Injected blocks never match: see :func:`typed_words`.

        Example:
            >>> evt.ctx.turn.matches(Clause(noun=Phrase("work"), verb=Phrase("stop", "halt")))
        """
        from captain_hook.signals.nlp import scan_text

        return scan_text(typed_words(self.user_text), patterns)
