from __future__ import annotations

import re
from typing import ClassVar

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Clause,
    CustomCondition,
    Event,
    InPlanMode,
    Input,
    NlpSignal,
    Phrase,
    RanCommand,
    Signal,
    Signals,
    T,
    Tool,
    Warn,
    llm_gate,
    llm_nudge,
    nudge,
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
    "Bugs, style violations, and broken tests in code you touch get fixed, not dismissed as pre-existing or "
    "out of scope. "
    "Fix the issue in this change before moving on.",
    skip_if=[TypeCheckerContext()],
    signals=Signals(
        [
            Signal(
                pattern=(
                    r"(?i)\b(?:pre-?existing|already (?:existed|present|broken)"
                    r"|(?:existing|known|old|previous) (?:issue|bug|problem|failure)s?)\b"
                ),
                weight=1,
            ),
            Signal(pattern=r"(?i)(?<!no change )(?<!no changes )(?:outside|beyond) (?:the )?scope", weight=1),
            NlpSignal(
                clauses=[
                    Clause(noun=Phrase.expand("change"), verb=Phrase("cause", "introduce"), negated=True),
                    Clause(
                        noun=Phrase("issue", "bug", "problem", "error", "failure", "violation", "warning"),
                        verb=Phrase("leave"),
                        tense="prospective",
                        subject=("unnamed",),
                    ),
                    Clause(
                        noun=Phrase("test"),
                        verb=Phrase("leave"),
                        adj=Phrase("broken", "failing", "flaky"),
                        tense="prospective",
                    ),
                ],
                weight=2,
            ),
        ],
        threshold=2,
        window=15,
        scope="text",
        vetoes=[
            Signal(pattern=r"(?i)\b(?:which|that)\s+leaves?\b"),
            Signal(pattern=r"(?i)\bleaving\b[^.!?]{0,80}?\bmeans?\b"),
        ],
    ),
    tests={
        Input(transcript=[T.assistant("Pre-existing, not caused by my changes.")]): Warn(),
        Input(transcript=[T.assistant("I found an issue and will fix it now.")]): Allow(),
        Input(transcript=[T.assistant("Pre-existing pyright type error, not caused by my changes.")]): Allow(),
        Input(transcript=[T.assistant("Pre-existing diagnostic from LSP, not my changes.")]): Allow(),
        Input(transcript=[T.assistant("No issues found in the code.")]): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "The pyright complaint here is the cached_property override one — "
                    "per AGENTS.md this is trivial noise, pre-existing, not worth a "
                    "type: ignore. Moving on to the actual feature work."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "When you edit an existing doc, fix tells only in the lines you're "
                    "already changing — never reflow pre-existing untouched lines to "
                    "satisfy the linter, which is scope creep over the author's "
                    "deliberate voice."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.user(
                    "These are pre-existing lines, and cleaning them is beyond the scope of my change.",
                    isMeta=True,
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant("These are pre-existing lines, and cleaning them is beyond the scope of my change.")
            ]
        ): Warn(),
        Input(
            transcript=[
                T.assistant(
                    "When you edit an existing doc, fix tells only in the lines you're "
                    "already changing — never reflow pre-existing untouched lines to "
                    "satisfy the linter, which is scope creep over the author's "
                    "deliberate voice."
                ),
                T.assistant("That refactor is beyond the scope of this task."),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.user("Let's just leave the login issue for now; it's pre-existing and beyond the scope of my change.")
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "Let's just leave the login issue for now; it's pre-existing and beyond the scope of my change."
                )
            ]
        ): Warn(),
        Input(
            transcript=[
                T.user(
                    "Review this transcript excerpt for whether the nudge should have fired:\n"
                    "> That's a pre-existing issue, not caused by my change. I'll leave the broken "
                    "test as is since it's beyond the scope of my change.\n"
                    "Report your assessment."
                ),
                T.assistant("Starting the review of that excerpt now."),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "TaskUpdate",
                        subject="Investigate steering nudge misfire",
                        description=(
                            "The pre-existing issue nudge fired again on a quoted transcript; "
                            "capturing the offending shape for the fix."
                        ),
                    )
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "Ignoring the pre-existing issue with the hook misfires for a moment — I'm "
                    "assessing the partial work to decide whether to salvage the existing progress "
                    "or rerun from scratch."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant("Reading the steering pack fixtures."),
                T.user(
                    "steering.py fixtures:\n"
                    "  \"Let's just leave the login issue for now; it's pre-existing and beyond "
                    'the scope of my change."  -> Warn()\n'
                    '  "That\'s a pre-existing issue, not caused by my change."  -> Warn()'
                ),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "Bugs, style violations, and broken tests in code you touch get fixed, not dismissed as "
                    "pre-existing or out of scope. Acknowledged — I'll actually fix it."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.user(
                    "That's a pre-existing issue, not caused by my change, and it's beyond the "
                    "scope of my change — I'll leave the broken test as is."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "Fixed the failing test and left the flaky one documented in the tracking "
                    "issue so we can revisit it deliberately."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "Landed the parser fix; I left the pre-existing lint warnings alone since they "
                    "predate this change, and all 42 tests pass."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant("That's a pre-existing issue, not caused by my change — I'm not going to touch it.")
            ]
        ): Warn(),
        Input(transcript=[T.assistant("I'll leave the broken test as is.")]): Warn(),
        Input(transcript=[T.assistant("I'll leave the flaky test as is.")]): Warn(),
        Input(transcript=[T.assistant("I'll leave the integration test running while I inspect its logs.")]): Allow(),
        Input(
            transcript=[T.assistant("I fixed all known issues and made no changes outside the scope of the task.")]
        ): Allow(),
        Input(transcript=[T.assistant("I should have left the broken test alone, but I fixed it.")]): Allow(),
        Input(transcript=[T.assistant("Leaving the test broken was never an option, so I fixed it.")]): Allow(),
        Input(transcript=[T.assistant("The previous issue is beyond the scope of this change.")]): Warn(),
        Input(transcript=[T.assistant("There are no unknown bugs left outside scope.")]): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "A mixed batch plus one transient S3 failure would leave a partition "
                    "looking more recently used than it was, inverting the LRU order this "
                    "PR exists to establish."
                )
            ]
        ): Allow(),
        Input(transcript=[T.assistant("I'll leave that failure unresolved for now.")]): Warn(),
        Input(
            transcript=[
                T.assistant(
                    "If it happens after the flag is set, the frame fails, triggering a redo-leg "
                    "failure without an abort, which leaves the poison unset and causes a flake."
                )
            ]
        ): Allow(),
        Input(transcript=[T.assistant("I'll leave that issue alone for now.")]): Warn(),
        Input(
            transcript=[
                T.assistant(
                    "There's also the CLAUDE.md issue: the hooks reference a Model Routing "
                    "section that lists opus as handling synthesis/accept-reject over review "
                    "findings, so leaving that section unedited means it contradicts the hooks "
                    "and could get reverted later."
                )
            ]
        ): Allow(),
        Input(transcript=[T.assistant("I'll leave that CLAUDE.md issue unaddressed for now.")]): Warn(),
    },
)


nudge(
    "Trivial type-checker warnings (`cached_property` overriding `property`, minor override mismatches, "
    "descriptor protocol) need no fix; only type issues that indicate real bugs do. "
    "Move on to the task without checking git history for who introduced them.",
    skip_if=[
        RanCommand("uv", "run", "ty", "check"),
        RanCommand("uvx", "ty", "check"),
        RanCommand("prek", "run", "ty"),
        RanCommand("prek", "run", "--all-files"),
        RanCommand("uvx", "prek", "run", "ty"),
        RanCommand("uvx", "prek", "run", "--all-files"),
        RanCommand("uvx", "pyright"),
    ],
    signals=Signals(
        [
            Signal(pattern=r"(?i)check\s+(?:the\s+)?git\s+(?:history|log|blame)", weight=2),
            Signal(pattern=r"(?i)(?:something|warnings?|errors?)\s+i\s+(?:introduced|added|caused)", weight=2),
            Signal(
                pattern=(
                    r"(?i)(?:existed|were\s+there|present)\s+(?:before|prior\s+to)\s+(?:my\s+)?(?:changes?|edits?)"
                ),
                weight=2,
            ),
            Signal(
                pattern=(r"(?i)warnings?\s+(?:are|is)?\s*(?:showing\s+up|appearing|popping\s+up)\s+(?:again|now|in)"),
                weight=2,
            ),
        ],
        threshold=4,
        window=10,
        scope="window",
        vetoes=[
            Signal(pattern=r"(?i)(?:actual|real|genuine)\s+(?:bug|error)"),
            Signal(pattern=r"(?i)wrong\s+(?:type|signature|return\s+type)"),
        ],
    ),
    tests={
        Input(
            transcript=[
                T.assistant("The warnings are showing up again in strict mode, which means pyright is catching them.")
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant("Let me check the git history to see if these pyright warnings existed before my changes.")
            ]
        ): Warn(),
        Input(
            transcript=[
                T.assistant("Let me check the git history."),
                T.assistant("The warnings are appearing again."),
            ]
        ): Warn(),
        Input(
            transcript=[T.assistant("Strict mode pyright is catching warnings — is this something I introduced?")]
        ): Allow(),
        Input(transcript=[T.assistant("The wrong return type is the actual bug — let me fix it.")]): Allow(),
        Input(transcript=[T.assistant("I'll fix this real type error in the engine.")]): Allow(),
        Input(transcript=[T.assistant("Let me check git history for the auth refactor.")]): Allow(),
        Input(
            transcript=[
                T.assistant(T.tool("Bash", command="uvx pyright src")),
                T.assistant("Let me check the git history to see if these pyright warnings existed before my changes."),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(T.tool("Bash", command="uvx ruff check src")),
                T.assistant("Let me check the git history to see if these pyright warnings existed before my changes."),
            ]
        ): Warn(),
    },
)


llm_nudge(
    """You are a senior engineer. Another engineer ("the agent") just submitted a plan via
ExitPlanMode. You are running in agent mode in the project's working directory, with read
tools. Your one job: decide whether the plan is a ROOT-CAUSE fix or a BAND-AID.

Read first, judge second:
- The session transcript is rendered above inside `<transcript evidence="...">`; long content is
  clipped (you'll see `…(+Nch)` markers). Use the supplied snapshot evidence for (1) the
  user's original request and (2) the plan the agent just submitted. Do not infer that a
  requirement or plan detail is absent when its evidence is clipped.
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
scope", "TODO", "revisit later"); hand-waving a symptom away with the environment; swapping the
requested fix for a softer deliverable (docs, help text, README, error-message copy) the user
never asked for; declaring the fix blocked because it "requires a release", a version bump, or a
change in another repo — releases are tag-driven and routine here, that is work to plan, not a
blocker.

Root-cause signs (lean fire=false): names the actual cause first; propagates/classifies errors
so callers fail fast; consolidates/removes code instead of adding a guarded branch; puts the
capability on the right abstraction; ships a test that exercises the REAL function and would
fail on today's code.

Do NOT fire when: the plan genuinely IS the root-cause fix; the task is small/cosmetic and a
direct fix is correctly sized (don't demand over-engineering); the plan adds a legitimate default
for a genuinely-unhandled case (a graceful default the user wanted is not symptom suppression —
e.g. "add a final fallback to launch in a new window instead of erroring"); the user explicitly
asked for this approach; or the plan reports a genuine hard blockage plainly (no access to the
other repo, missing credentials, an API that does not exist) and asks the user how to proceed.

When uncertain, return fire=false. A missed band-aid costs nothing; a false alarm on a sound plan
trains the agent to ignore this nudge. Fire only when a specific tell is clearly present and no
"do not fire" condition applies. Put your reasoning (under 60 words, ending with the one tell that
decided it) in `reasoning`.""",
    message=(
        "This plan treats the symptom instead of removing the cause, and a needed release, version bump, "
        "or cross-repo change is part of the fix. Revise the plan to name the root cause and make the "
        "failure impossible instead of catching, retrying, or suppressing it."
    ),
    only_if=[Tool("ExitPlanMode")],
    events=Event.PostToolUse,
    max_fires=1,
)


llm_gate(
    """You are a senior engineer. Another engineer ("the agent") is ending its turn or has just
produced deferral-shaped content mid-work (a thinking block, a review finding, a task or todo it
filed). You are running in agent mode in the project's working directory, with read tools. Your
one job: decide whether the agent DELIVERED the fix the user asked for, or silently DOWNGRADED
the deliverable while the real fix stays undone.

Read first, judge second:
- The session transcript is rendered above inside `<transcript evidence="...">`; long content is
  clipped (you'll see `…(+Nch)` markers). Use the supplied snapshot evidence; clipped
  passages cannot establish absence. You need: (1) what the user actually asked
  for (including any later approval of a reduced scope), (2) what the agent actually changed
  this turn, and (3) the agent's justification (the flagged lines are in `<context>`). On a
  mid-turn firing the flagged content may be deliberation in flight — check whether the agent
  has COMMITTED to the downgrade (substitute edits landed, a softer task filed as the plan of
  record, the real fix abandoned), not merely weighed it.
- Then inspect the working directory enough to confirm whether the requested fix was made or
  only a softer substitute (docs, help text, error copy) shipped.

Discriminator: Did the agent do the fix the user asked for (or get the user's explicit go-ahead
for something smaller) — or did it declare the real fix out of reach and substitute a softer
deliverable without asking, leaving the reported problem in place? Delivered or user-approved
-> block=false. Silent downgrade -> block=true. "Silent" is the whole test: a turn that ends by
asking the user how to proceed on the fix — a question, or a menu of concrete options — is
never silent, so it is block=false no matter what the turn said before the question. The
question is the sanctioned escape hatch; it is never evidence for firing, and neither is the
diagnosis, workaround, or unfixed state the turn describes on its way to that question.

Deferral tells (lean block=true): names the correct fix, then declares it blocked because it
"requires a release", a version bump, or an upstream/cross-repo change — in this ecosystem
releases are tag-driven and routine, so that is work to plan, not a blocker; swaps the fix for
documentation, help text, README, or error-message copy the user never asked for;
"practical"/"pragmatic" framing used to justify the smaller deliverable; "for now" / "as a
workaround" / "stopgap" / "interim" language around what shipped; punts to a follow-up PR,
future work, or "file an issue" the user didn't request; declares the work done while its own
transcript shows the reported failure unaddressed and untested.

Legitimate-deferral signs (lean block=false): the agent surfaced the blocker and ASKED the user
how to proceed before substituting anything; the softer deliverable IS the task the user
requested; the user approved a plan or message that explicitly named the smaller deliverable (an
approved ExitPlanMode plan counts as the go-ahead); a genuine hard blockage the agent reported
plainly (no access to the other repo, missing credentials, an API that does not exist); the
agent shipped the real fix AND improved docs alongside it; deliberation that names the softer
option in order to reject it and commits to the real fix; a finding or quoted text reporting
the codebase's or someone else's existing deferral debt or a genuine upstream bug — reporting
is not deferring, UNLESS the same content prescribes the downgrade as the remediation (e.g.
"fix requires a release, so document the constraint instead"); a task recording a genuine
environmental constraint that replaces no requested fix.

Do NOT fire when: the turn ends in a question to the user about the blocker or the fix — "want
me to apply a durable fix?", "the options are: …", "which do you prefer?" — asking is the
sanctioned escape hatch, not laziness, and a workaround named or a fix left undone BEFORE that
question is what the question is about, not a downgrade committed to; the user explicitly asked
for the docs/help-text/error-copy change; the deferral language refers to genuinely optional
extras after the requested fix landed; the flagged content is mid-turn deliberation where no
substitute has been executed yet; or the blockage is real, outside the agent's reach, and
clearly reported rather than papered over.

When uncertain, return block=false. A missed deferral costs one nag; a false alarm on an honest
stop teaches the agent to ignore this gate. Fire only when a specific tell is clearly present
and no "do not fire" condition applies. Put your reasoning (under 60 words, ending with the one
tell that decided it) in `reasoning`.""",
    message=(
        "Deliver the requested fix instead of substituting docs, help text, or a follow-up issue; a "
        "release, version bump, or cross-repo change is part of that fix, not a blocker. If you are "
        "genuinely blocked, end the turn by asking how to proceed."
    ),
    signals=Signals(
        [
            Signal(
                pattern=(
                    r"(?i)(?:requires?|needs?|blocked\s+on|waiting\s+(?:for|on))\s+(?:a\s+|an\s+)?(?:new\s+)?"
                    r"(?:[\w.-]+\s+)?(?:release|version\s+bump|upstream\s+(?:change|fix|release))"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:(?:until|once|unless|before)\s+(?:\w+[\w.'-]*\s+){0,3}?|(?:ha(?:s|ve)|need(?:s)?)\s+to\s+"
                    r"|(?:fix|solution|answer)\s+(?:is|would\s+be)\s+to\s+)(?:cut|ship|tag|publish|releas|bump)\w*"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:fix|change)\s+(?:really\s+)?(?:lives?|belongs?|is|sits?|happens?"
                    r"|would\s+have\s+to\s+happen)\s+(?:in|on)\s+(?:another|a\s+(?:different|separate)|the\s+[\w.-]+)"
                    r"\s+(?:repo(?:sitory)?|package|project|codebase|side|library|end)"
                ),
                weight=2,
            ),
            Signal(pattern=r"(?i)upstream\s+(?:problem|issue|bug|limitation)", weight=2),
            Signal(
                pattern=(
                    r"(?i)(?:practical|pragmatic|expedient|sensible|simplest)\s+(?:solution|approach|fix|option|path)"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)\b(?:improv|updat|expand|clarif|document|add)\w*\s+(?:the\s+|this\s+|that\s+|a\s+)?"
                    r"(?:documentation|docs\b|help\s+text|readme|error\s+(?:message|copy|text))"
                ),
                weight=1,
            ),
            Signal(
                pattern=(
                    r"(?i)\b(?:document\w*|not(?:e|es|ed|ing)|record\w*)\s+(?:the\s+|this\s+|that\s+|a\s+|its\s+)?"
                    r"(?:[\w'-]+\s+){0,2}?(?:limitation|caveat|constraint|shortcoming|known\s+issue|behavior)"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:(?:left|leaves?|leaving)\s+(?:the\s+|it\s+|this\s+|that\s+)?"
                    r"(?:code|implementation|behavior|bug|issue|it)?\s*(?:as-?is|untouched|unchanged|in\s+place)"
                    r"|still\s+(?:present|broken|unfixed|unaddressed|there\b|reproduc\w+)"
                    r"|remains?\s+(?:broken|unfixed|in\s+place))"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:instead\s+of|rather\s+than)\s+"
                    r"(?:fix|chang|touch|releas|rewrit|redesign|refactor|overhaul)\w*"
                ),
                weight=1,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:workaround|stop-?gap|interim\s+(?:fix|solution|measure)|band-?aid"
                    r"|(?:as\s+a\s+)?temporary\s+(?:measure|fix|solution|patch|guard)"
                    r"|in\s+the\s+meantime|for\s+the\s+time\s+being)"
                ),
                weight=2,
            ),
            Signal(pattern=r"(?i)\bfor\s+now\b", weight=1),
            Signal(
                pattern=(
                    r"(?i)(?:follow-?up\s+(?:pr|work|issue|task|change)|(?:in|as)\s+a\s+follow-?up\b"
                    r"|future\s+(?:pr|work|release)"
                    r"|file\s+(?:an?\s+)?issue|(?:separate|later|subsequent)\s+(?:pr|patch|pass|iteration)|backlog"
                    r"|out\s+of\s+scope\s+for\s+this)"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:out\s+of\s+my\s+(?:hands|control)|nothing\s+(?:i|we)\s+can\s+(?:do|change|fix)"
                    r"|left\s+a\s+(?:short\s+)?note)"
                ),
                weight=1,
            ),
            Signal(
                pattern=(
                    r"(?i)(?:(?:smaller|minimal|narrow)\s+(?:change|patch|fix)|silenc\w*\s+the\s+(?:warning|error)"
                    r"|so\s+(?:it|this|that|the\s+\w+)\s+(?:no\s+longer|doesn['’]t|does\s+not|won['’]t)\s+"
                    r"(?:crash|die|fail|error)\w*)"
                ),
                weight=2,
            ),
            Signal(
                pattern=(
                    r"(?i)\b(?:punt|defer|postpon)\w*\s+(?:the|this|that|it)\b"
                    r"|\bskip\w*\s+the\s+(?:real|proper|actual|root)\b"
                ),
                weight=2,
            ),
            Signal(
                pattern=r"(?i)\brevisit\s+(?:it\s+|this\s+)?later\b|\bcome\s+back\s+to\s+(?:it|this)\b",
                weight=1,
            ),
            Signal(pattern=r"(?i)\bmov(?:e[ds]?|ing)\s+on\b", weight=1),
            NlpSignal(
                clauses=[Clause(noun=Phrase.expand("fix"), verb=Phrase("defer", "postpone", "punt"))],
                weight=2,
            ),
        ],
        threshold=3,
        window="turn",
        scope="window",
        vetoes=[
            Signal(
                pattern=(
                    r"(?i)(?:ask(?:ed|ing)?\s+the\s+user|how\s+would\s+you\s+like\s+to\s+proceed|should\s+i\s+proceed"
                    r"|do\s+you\s+want\s+me\s+to|would\s+you\s+(?:prefer|rather|like)|which\s+(?:option|approach)\b)"
                ),
            ),
            Signal(
                pattern=(
                    r"(?is)\b(?:want\s+me\s+to|shall\s+i|which\s+(?:option|approach|one)|would\s+you\s+(?:prefer|rather|like))\b"
                    r"[^?\n]*\?(?:[ \t]*[^\n?]*:)?(?:\s*(?:[-*•]|\d+[.)])[^\n]*)*\s*\Z"
                ),
            ),
            Signal(
                pattern=(
                    r"(?i)(?:as\s+(?:you\s+)?requested|you\s+asked\s+(?:for|me)\b|(?:as\s+)?we\s+(?:discussed|agreed))"
                ),
            ),
        ],
    ),
    events=Event.PostToolUse | Event.Stop | Event.SubagentStop,
    skip_if=[InPlanMode()],
    tests={
        Input(
            transcript=[
                T.assistant(
                    "Since fixing this requires a vcs-core release, the practical "
                    "solution is to improve the documentation and help text."
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "The vcs.Root error still happens outside a repo. For now, as a "
                    "workaround, I've added a note to the help text telling users to "
                    "run these commands from the repo root."
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Fixing this properly requires an upstream release of vcs-core, "
                    "so the pragmatic approach is to document the limitation in the README."
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Fixing this properly requires a vcs-core release. Do you want "
                    "me to cut that release, or would you prefer to just document the "
                    "limitation for now?"
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.user("Please improve the documentation for the release commands."),
                T.assistant("Done — I improved the documentation and help text as you asked me to."),
            ]
        ): Allow(),
        Input(transcript=[T.assistant("I refactored the signal scoring in state.py and all 42 tests pass.")]): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "The underlying bug is in the wheel we publish, so the clean fix is "
                    "to ship a new version to PyPI. I've left the code as-is for callers "
                    "to pin around."
                )
            ]
        ): Block(),
        Input(transcript=[T.assistant("I documented the limitation and moved on to the next task.")]): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Fixing this properly needs an upstream release and the pragmatic "
                    "approach is to document the limitation — do you want me to just "
                    "leave that as-is?"
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "I expanded the README troubleshooting section as we discussed and "
                    "filed a follow-up task to document the new error codes."
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "Fixing this cleanly needs a new logparse release. Do you want "
                    "me to cut that release, or handle it another way?"
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "ReportFindings",
                        findings=[
                            {
                                "file": "internal/daemon/daemon.go",
                                "line": 105,
                                "summary": (
                                    "OpClose --stale and OpList are documented as cross-repo "
                                    "commands, but vcs-core's dispatch runs ScopeResolve "
                                    "(vcs.Root) on the caller's cwd for every domain op with a "
                                    "hardcoded exemption set, so both error when invoked "
                                    "outside a git/jj repo."
                                ),
                                "failure_scenario": (
                                    "User cd ~ && reviewctl list → error from vcs.Root instead "
                                    "of the cross-repo listing; fix requires a vcs-core "
                                    "release, so reviewctl documents the run-inside-a-repo "
                                    "constraint instead."
                                ),
                                "verdict": "CONFIRMED",
                            }
                        ],
                    )
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    T.thinking(
                        "I'm seeing that vcs-core has hardcoded exemptions for certain "
                        "operations, which means any domain operation without an exemption "
                        "gets an error when the scope can't be resolved. The issue is that "
                        "`reviewctl list` and `close --stale` commands don't have consumer "
                        "hooks to exempt them, so running these from outside a repo directory "
                        "triggers a vcs.Root error. Since fixing this requires a vcs-core "
                        "release, the practical solution is to improve the documentation and "
                        "help text to guide users on where to run these commands from."
                    )
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Since fixing this requires a vcs-core release, the practical "
                    "solution is to document the limitation in the help text."
                ),
                *(line for i in range(6) for line in T.tool_turn("Read", file_path=f"/tmp/f{i}.py")),
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "ReportFindings",
                        findings=[
                            {
                                "file": "captain_hook/signals/__init__.py",
                                "line": 46,
                                "summary": (
                                    "Off-by-one in windowed() drops the final event when stop lands on a turn boundary."
                                ),
                                "failure_scenario": (
                                    "Session.recent(1) on a three-event transcript returns an "
                                    "empty window, so signal scoring sees no text."
                                ),
                                "verdict": "CONFIRMED",
                            }
                        ],
                    )
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "TodoWrite",
                        todos=[
                            {
                                "content": "Add SSE reconnect test",
                                "status": "pending",
                                "activeForm": "Adding SSE reconnect test",
                            },
                            {
                                "content": "Wire the backoff cap into the client config",
                                "status": "in_progress",
                                "activeForm": "Wiring the backoff cap",
                            },
                        ],
                    )
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "TaskCreate",
                        subject="Workaround for the crash",
                        description="Wrap the call in try/except so the session survives; revisit later.",
                    )
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "TodoWrite",
                        todos=[
                            {
                                "content": "Defer the parser rewrite to a later pass",
                                "status": "pending",
                                "activeForm": "Deferring the parser rewrite",
                            },
                        ],
                    )
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "TaskCreate",
                        subject="Skip the real fix",
                        description="Too big for this pass; just note it somewhere and come back to it.",
                    )
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "ReportFindings",
                        findings=[
                            {
                                "file": "captain_hook/signals/__init__.py",
                                "line": 90,
                                "summary": (
                                    "The `document the limitation` fallback branch in "
                                    "cite_message is dead code — no caller reaches it."
                                ),
                                "failure_scenario": (
                                    "extract_signal_context always returns a non-empty list "
                                    "for these patterns, so the else arm never runs."
                                ),
                                "verdict": "CONFIRMED",
                            }
                        ],
                    )
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "TaskCreate",
                        subject="Document the limitation",
                        description=(
                            "Note in the README that NLP scoring needs the spaCy en_core_web_sm model provisioned."
                        ),
                    )
                )
            ]
        ): Allow(),
        Input(
            transcript=[
                T.user("why are the build hooks timing out, the linter timing out, and SessionStart failing?"),
                T.assistant(
                    T.thinking(
                        "The daemon has been up for a day, but checking its environment via ps didn't return "
                        "anything useful. I'll move on and check the registry priority instead to confirm the "
                        "right backend is selected and see if there's a fallback mechanism on failure."
                    )
                ),
                T.assistant(
                    "Three separate causes, not one.\n\n## 1. hook `context deadline exceeded` — OAuth token "
                    "refresh storm\n\nDozens of concurrent worker processes share one rotating refresh token "
                    "with no cross-process lock; each failed call burns seconds before raising, which blows the "
                    "hook client's socket deadline. It self-healed once one refresh won.\n\n"
                    "## 2. linter `timed out after 2m0s` — load, not auth\n\nWorkaround is the known one: "
                    "`--sentence-timeout=6m`, or `--llm-effort=off` when you only want the client-side rules.\n\n"
                    "## 3. SessionStart PATH-probe fault — mostly cosmetic now\n\nThe launchd plist already "
                    "pins a PATH that resolves the worker binaries, so the probe failing is harmless.\n\n---\n\n"
                    "Want me to apply a durable fix for #1? The options:\n"
                    "- Point the hook's review specialty at a different backend.\n"
                    "- Wrap the worker dispatch in a `flock` on the auth file so refreshes serialize.\n"
                    "- File it upstream against the worker library and leave it — it self-heals each hour."
                ),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.user("why is the linter timing out? fix it"),
                T.assistant(
                    "The sentence pass still times out under load. Workaround is the known one: I bumped "
                    "`--sentence-timeout` to 6m in the wrapper and left the startup cost as-is; the real fix "
                    "lives in the linter repo."
                ),
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Compiler options: --strict and --no-emit. The fix requires a release. I updated the "
                    "documentation instead of fixing it; the bug remains unfixed."
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Shall I cut the release? No — the pragmatic solution is to document the limitation in the "
                    "README for now; the bug remains unfixed."
                )
            ]
        ): Block(),
        Input(
            transcript=[
                T.assistant(
                    "Fixing this needs a logparse release, so for now I documented the limitation. "
                    "Want me to cut that release?\n- yes, cut it now\n- no, leave the note"
                )
            ]
        ): Allow(),
    },
)
