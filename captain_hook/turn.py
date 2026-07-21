from __future__ import annotations

from typing import TYPE_CHECKING

from cc_transcript.query import Session

if TYPE_CHECKING:
    from captain_hook.signals.nlp import Clause


class Turn(Session):
    """The current turn as a one-turn :class:`~cc_transcript.query.Session` view.

    What ``evt.ctx.turn`` returns: the full windowed transcript surface
    (``user_text``, ``has_tool``, ``tool_calls``, …) plus prompt matching, so
    hooks can test the turn's opening prompt without importing the NLP machinery.
    """

    __slots__ = ()

    def matches(self, *patterns: str | Clause) -> bool:
        """Whether the turn's opening prompt matches any pattern.

        A string pattern is a case-insensitive regex; a
        :class:`~captain_hook.Clause` runs the dependency-clause scan.

        Example:
            >>> evt.ctx.turn.matches(Clause(noun=Phrase("work"), verb=Phrase("stop", "halt")))
        """
        from captain_hook.signals.nlp import scan_text

        return scan_text(self.user_text, patterns)
