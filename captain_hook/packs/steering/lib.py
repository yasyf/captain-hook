"""Side-effect-free building blocks for the two steering nudges.

Carries ``__capt_hook_skip__ = True`` so the discovery loader treats it as a
declared library, not an auto-loaded hook module: importing it registers nothing.
``steering.py`` (and any consumer) imports the builders from here and calls them
with its own message/skip/tests to register the nudges.
"""

from __future__ import annotations

import re
from typing import ClassVar

from captain_hook import (
    BaseHookEvent,
    Clause,
    CustomCondition,
    InlineTests,
    NlpSignal,
    Phrase,
    RanCommand,
    Signal,
    Signals,
    nudge,
)

__capt_hook_skip__ = True

PRE_EXISTING_SIGNALS = Signals(
    [
        Signal(pattern=r"(?i)(?:pre-existing|preexisting)", weight=2),
        Signal(pattern=r"(?i)(?:outside|beyond) (?:the )?scope", weight=1),
        NlpSignal(
            clauses=[
                Clause(noun=Phrase.expand("change"), verb=Phrase("cause", "introduce"), negated=True),
                Clause(noun=Phrase.expand("issue"), verb=Phrase("leave")),
            ],
            weight=2,
        ),
        NlpSignal(
            clauses=[
                Clause(noun=Phrase.expand("issue"), adj=Phrase("existing", "present", "previous")),
            ],
            weight=1,
        ),
    ],
    threshold=2,
    window=15,
)

TRIVIAL_TYPE_SIGNALS = Signals(
    [
        Signal(
            pattern=r"(?i)check\s+(?:the\s+)?git\s+(?:history|log|blame)",
            weight=2,
        ),
        Signal(
            pattern=r"(?i)(?:something|warnings?|errors?)\s+i\s+(?:introduced|added|caused)",
            weight=2,
        ),
        Signal(
            pattern=(
                r"(?i)(?:existed|were\s+there|present)\s+(?:before|prior\s+to)\s+"
                r"(?:my\s+)?(?:changes?|edits?)"
            ),
            weight=2,
        ),
        Signal(
            pattern=(
                r"(?i)warnings?\s+(?:are|is)?\s*(?:showing\s+up|appearing|popping\s+up)\s+"
                r"(?:again|now|in)"
            ),
            weight=2,
        ),
        Signal(pattern=r"(?i)(?:actual|real|genuine)\s+(?:bug|error)", weight=-3),
        Signal(pattern=r"(?i)wrong\s+(?:type|signature|return\s+type)", weight=-3),
    ],
    threshold=4,
    window=10,
)


class TypeCheckerContext(CustomCondition):
    """True when the recent assistant transcript is discussing a type checker / diagnostics."""

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)(?:\b(?:pyright|mypy|type.?check(?:ing)?|type.?error|type.?annotation"
        r"|type.?warning|type.?issue|type.?mismatch|diagnostics?|lsp"
        r"|could not be resolved|possibly unbound|cannot be assigned)\b"
        r"|TYPE_CHECKING|#\s*type:\s*ignore)"
    )

    def check(self, evt: BaseHookEvent) -> bool:
        return bool((t := evt.ctx.transcript) and self.PATTERN.search(t.assistant_text(n=10)))


def pre_existing_nudge(*, message: str, tests: InlineTests) -> None:
    """Register the pre-existing-issue nudge, skipped while the transcript is discussing a type checker."""
    nudge(message, skip_if=[TypeCheckerContext()], signals=PRE_EXISTING_SIGNALS, tests=tests)


def trivial_type_nudge(*, message: str, skip: str, tests: InlineTests) -> None:
    """Register the trivial-type nudge, skipped when ``skip`` (a RanCommand pattern) was already run."""
    nudge(message, skip_if=[RanCommand(skip)], signals=TRIVIAL_TYPE_SIGNALS, tests=tests)
