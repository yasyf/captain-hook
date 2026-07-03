from __future__ import annotations

import re
from typing import ClassVar

from captain_hook import (
    Allow,
    BaseHookEvent,
    Clause,
    CustomCondition,
    Event,
    Input,
    NlpSignal,
    Phrase,
    RanCommand,
    Signal,
    Signals,
    Tool,
    Warn,
    llm_nudge,
    nudge,
)

PRE_EXISTING_SIGNALS = Signals(
    [
        Signal(pattern=r"(?i)(?:pre-existing|preexisting)", weight=1),
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
        Signal(pattern=r"(?i)check\s+(?:the\s+)?git\s+(?:history|log|blame)", weight=2),
        Signal(pattern=r"(?i)(?:something|warnings?|errors?)\s+i\s+(?:introduced|added|caused)", weight=2),
        Signal(
            pattern=(r"(?i)(?:existed|were\s+there|present)\s+(?:before|prior\s+to)\s+(?:my\s+)?(?:changes?|edits?)"),
            weight=2,
        ),
        Signal(
            pattern=(r"(?i)warnings?\s+(?:are|is)?\s*(?:showing\s+up|appearing|popping\s+up)\s+(?:again|now|in)"),
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


nudge(
    "You appear to be dismissing a pre-existing issue rather than fixing it. "
    "Leave the codebase better than you found it — if you encounter a bug, style "
    "violation, or broken test in code you're touching, fix it. Don't rationalize "
    "skipping it as out of scope. See: AGENTS.md § Code Stewardship.",
    skip_if=[TypeCheckerContext()],
    signals=PRE_EXISTING_SIGNALS,
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
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "When you edit an existing doc, fix tells only in the lines you're "
                                    "already changing — never reflow pre-existing untouched lines to "
                                    "satisfy the linter, which is scope creep over the author's "
                                    "deliberate voice."
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
    skip_if=[
        RanCommand("uv", "run", "ty", "check"),
        RanCommand("uvx", "ty", "check"),
        RanCommand("prek", "run", "ty"),
        RanCommand("prek", "run", "--all-files"),
        RanCommand("uvx", "prek", "run", "ty"),
        RanCommand("uvx", "prek", "run", "--all-files"),
        RanCommand("uvx", "pyright"),
    ],
    signals=TRIVIAL_TYPE_SIGNALS,
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
                                "text": ("Strict mode pyright is catching warnings — is this something I introduced?"),
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


llm_nudge(
    """You are a senior engineer. Another engineer ("the agent") just submitted a plan via
ExitPlanMode. You are running in agent mode in the project's working directory, with read
tools. Your one job: decide whether the plan is a ROOT-CAUSE fix or a BAND-AID.

Read first, judge second:
- The session transcript is rendered above inside `<transcript path="...">`; long content is
  clipped (you'll see `…(+Nch)` markers). Read the FULL plan and opening request from that path
  (prefer `cc-transcript show`/`grep`; else read the file). You need: (1) the user's ORIGINAL
  request (the first user message) and (2) the PLAN the agent just submitted (the most recent
  ExitPlanMode plan; read it in FULL).
- Then inspect the cited code in the working directory enough to tell whether the plan removes
  the cause or only treats the symptom.

Discriminator: Does the plan make the failure IMPOSSIBLE — remove its cause, propagate/classify
the condition, fix the general computation — or merely INVISIBLE or survivable-for-now: catch,
default, retry, suppress, defer, special-case, or bolt a parallel path onto an existing
primitive? Impossible -> fire=false. Invisible/survivable -> fire=true.

Band-aid tells (lean fire=true): returns a sentinel/default on error instead of propagating
(esp. when a downstream loop trusts it); blanket suppression to silence one complaint
(`--no-verify`, broad `except`, `# type: ignore`, `--force`); unbounded "retry forever" /
bumping a timeout / a sleep against a failure that can't succeed; hardcoding or special-casing
the one failing input; bolting a new parallel form/flag/mode onto a primitive instead of putting
the capability on the shared abstraction; changing a test to match buggy behavior or stubbing
the broken function; defensive theater that hides rather than removes the cause (the real fix
often DELETES code); deferral language used to dodge the fix ("for now", "stopgap", "out of
scope", "TODO", "revisit later"); hand-waving a symptom away with the environment.

Root-cause signs (lean fire=false): names the actual cause first; propagates/classifies errors
so callers fail fast; consolidates/removes code instead of adding a guarded branch; puts the
capability on the right abstraction; ships a test that exercises the REAL function and would
fail on today's code.

Do NOT fire when: the plan genuinely IS the root-cause fix; the task is small/cosmetic and a
direct fix is correctly sized (don't demand over-engineering); the plan adds a legitimate default
for a genuinely-unhandled case (a graceful default the user wanted is not symptom suppression —
e.g. "add a final fallback to launch in a new window instead of erroring"); or the user
explicitly asked for this approach.

When uncertain, return fire=false. A missed band-aid costs nothing; a false alarm on a sound plan
trains the agent to ignore this nudge. Fire only when a specific tell is clearly present and no
"do not fire" condition applies. Put your reasoning (under 60 words, ending with the one tell that
decided it) in `reasoning`.""",
    message=lambda r: (
        "This plan looks like a band-aid — it treats the symptom rather than removing the "
        f"cause of the problem you were asked to solve. Why: {r.reasoning} "
        "Re-derive from first principles: name the actual root cause, then make the failure "
        "impossible (propagate/classify the error, fix the general computation, or delete the "
        "code that creates it) rather than catching, defaulting, retrying, suppressing, or "
        "special-casing it. If a small/direct fix is genuinely correct here, or the user asked "
        "for this approach, proceed."
    ),
    only_if=[Tool("ExitPlanMode")],
    events=Event.PostToolUse,
    max_fires=1,
)
