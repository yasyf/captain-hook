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
    RanCommand,
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


nudge(
    "Stop investigating trivial pyright/typing warnings. Per AGENTS.md § General Rules — "
    "Don't contort code to satisfy a checker: ignore trivial type issues (`cached_property` "
    "overriding `property`, minor override mismatches, descriptor protocol). Only fix type "
    "issues that indicate actual bugs. Don't check git history to see if you introduced "
    "them — move on.",
    signals=Signals(
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
    ),
    skip_if=[RanCommand(r"(?:uv run ty check|uvx pyright)")],
    tests={
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The warnings are showing up again in strict mode, "
                                    "which means pyright is catching them."
                                ),
                            },
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
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Let me check the git history to see if these pyright "
                                    "warnings existed before my changes."
                                ),
                            },
                        ]
                    },
                }
            ]
        ): Warn(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Strict mode pyright is catching warnings — "
                                    "is this something I introduced?"
                                ),
                            },
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
                        "content": [
                            {
                                "type": "text",
                                "text": "The wrong return type is the actual bug — let me fix it.",
                            },
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
                        "content": [
                            {
                                "type": "text",
                                "text": "I'll fix this real type error in the engine.",
                            },
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
                        "content": [
                            {
                                "type": "text",
                                "text": "Let me check git history for the auth refactor.",
                            },
                        ]
                    },
                }
            ]
        ): Allow(),
    },
)
