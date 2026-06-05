from __future__ import annotations

import re
from typing import ClassVar

from captain_hook import (
    Allow,
    BaseHookEvent,
    Clause,
    CustomCondition,
    Input,
    NlpSignal,
    Phrase,
    Signal,
    Signals,
    Warn,
    nudge,
)


class TypeCheckerContext(CustomCondition):
    PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)(?:\b(?:pyright|mypy|type.?check(?:ing)?|type.?error|type.?annotation"
        r"|type.?warning|type.?issue|type.?mismatch|diagnostics?|lsp"
        r"|could not be resolved|possibly unbound|cannot be assigned)\b"
        r"|TYPE_CHECKING|#\s*type:\s*ignore)"
    )

    def check(self, evt: BaseHookEvent) -> bool:
        return bool((t := evt.ctx.transcript) and self.PATTERN.search(t.assistant_text(n=10)))


nudge(
    "You appear to be dismissing a pre-existing issue rather than fixing it. "
    "Leave the codebase better than you found it — if you encounter a bug, style "
    "violation, or broken test in code you're touching, fix it. Don't rationalize "
    "skipping it as out of scope. See: AGENTS.md § Code Stewardship.",
    skip_if=[TypeCheckerContext()],
    signals=Signals(
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
    ),
    tests={
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Pre-existing, not caused by my changes."}]},
                }
            ]
        ): Warn(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "I found an issue and will fix it now."}]},
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Pre-existing pyright type error, not caused by my changes."}
                        ]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Pre-existing diagnostic from LSP, not my changes."}]
                    },
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "No issues found in the code."}]},
                }
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The pyright complaint here is the cached_property override one — "
                                    "per AGENTS.md this is trivial noise, pre-existing, not worth a "
                                    "type: ignore. Moving on to the actual feature work."
                                ),
                            }
                        ]
                    },
                }
            ]
        ): Allow(),
    },
)
