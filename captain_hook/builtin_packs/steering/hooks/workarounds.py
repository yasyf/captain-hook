from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import (
    COMMENT_TYPES,
    Allow,
    Block,
    Clause,
    Event,
    Input,
    Introduced,
    NlpSignal,
    Phrase,
    Signal,
    Signals,
    Tool,
    Warn,
    llm_gate,
    llm_nudge,
)

if TYPE_CHECKING:
    from collections.abc import Set

SIBLING_DEPS = re.compile(
    r"\b(?:cc[-_](?:transcript|context|notes|pushback)|ccx|spawnllm|capt[-_]hook|captain_hook)\b",
    re.IGNORECASE,
)

WORKAROUND_LEXICON = re.compile(
    r"work[-\s]?around"
    r"|\bshim\b"
    r"|band[-\s]?aid"
    r"|until\s+\S+\s+(?:ship|support|land|expose|grow|gain)"
    r"|\bfor\s+(?:now|the\s+time\s+being)\b"
    r"|\bin\s+the\s+meantime\b"
    r"|\btemporar(?:y|ily)\b"
    r"|does(?:n['’]t|\s+not)\s+support"
    r"|compat(?:ibility)?[-\s]layer"
    r"|\bupstream\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WorkaroundComments(Introduced):
    """Gating context: comments the pending edit newly introduces that read like a first-party workaround."""

    kind: str | Set[str] | None = COMMENT_TYPES

    def keep(self, text: str) -> bool:
        return bool(WORKAROUND_LEXICON.search(text) or SIBLING_DEPS.search(text))


llm_nudge(
    """Decide whether the pending edit introduces a comment marking a WORKAROUND for a
first-party dependency — consumer-side code accommodating a gap, quirk, or breaking
change in a sibling repo this ecosystem owns (cc-transcript, cc-context/ccx, cc-notes,
spawnllm, and the other cc-family repos) instead of fixing that dependency.

First-party dependencies live as sibling repos under ~/Code with tag-driven releases,
so "fix it upstream" is routine work, not a blocker: the right move is a change in the
dependency plus a version bump, and the workaround never gets written.

<workaround_comments> holds the comments this edit newly introduces that the scan
flagged as suspects. <before_edit> and <after_edit> hold the edit's old and new text
for surrounding context.

The test: does the comment explain code that exists only because a first-party
dependency lacks, breaks, or mishandles something — a missing constructor or accessor,
a shape change, an undocumented contract the consumer must assume? Accommodating code
plus a first-party subject earns fire=true. A workaround for a third-party library, a
platform or stdlib difference, or a deliberate degradation the code owns earns
fire=false.

<examples>
<example fire="true">
# cc-transcript views aren't constructible from Python, so round-trip through the parser
Consumer-side scar tissue for a first-party gap — the dependency should grow the constructor.
</example>
<example fire="true">
# TODO: delete once ccx supports resume-from-offset
Names the upstream fix and defers it; the sibling repo is where this change belongs.
</example>
<example fire="false">
# ast-grep-py find_all rejects bare strings — wrap in a rule dict
Third-party dependency; no sibling repo to fix. A local accommodation is correct.
</example>
<example fire="false">
# fall back to git when the ccx binary is absent
Intentional graceful degradation the code owns, not a gap in the dependency.
</example>
<example fire="false">
# /var/folders is the macOS temp root; /tmp on Linux
Platform branching, not a dependency workaround.
</example>
</examples>

When uncertain whether the subject is first-party or the accommodation is deliberate,
return fire=false. Put your reasoning (under 40 words, naming the dependency and the
gap) in `reasoning`.""",
    message=(
        "This edit works around a first-party dependency instead of fixing it. "
        "Why: {reasoning} "
        "The dependency is a sibling repo with tag-driven releases — fix the primitive "
        "there: add the missing surface upstream, bump the pin, and skip the accommodation. "
        "If a local workaround is genuinely correct here, say why in your reply and proceed."
    ),
    label="upstream_workaround_edit",
    contexts=[WorkaroundComments()],
    events=Event.PreToolUse,
    only_if=[Tool("Edit", "Write", "MultiEdit")],
    agent=False,
    transcript=False,
    tests={
        Input(
            file="src/consumer.py",
            old="event = build_event(raw)\n",
            content=(
                "# cc-transcript has no synthetic event constructor, so re-parse a hand-built envelope\n"
                "event = parse(json.dumps(raw))\n"
            ),
        ): Warn(pattern="first-party"),
        Input(
            file="src/scan.py",
            old="find_all(pat)\n",
            content="# ast-grep-py rejects bare string patterns; wrap in a rule dict\nfind_all({'rule': pat})\n",
        ): Allow(),
        Input(
            file="src/vcs.py",
            old="diff = ccx_diff()\n",
            content="# fall back to git when the ccx binary is absent\ndiff = ccx_diff() or git_diff()\n",
            llm={"fire": False},
        ): Allow(),
    },
)


llm_gate(
    """You are a senior engineer. Another engineer ("the agent") is ending its turn. You are
running in agent mode in the project's working directory, with read tools. Your one
job: decide whether this turn LANDED a consumer-side workaround for a first-party
dependency instead of fixing the dependency itself.

First-party means a dependency this ecosystem owns: it lives as a sibling repo (check
with `ccx repo locate <name>` — a `repo` row means first-party) and its releases are
tag-driven and routine. cc-transcript, cc-context, cc-notes, spawnllm, and the other
cc-family repos qualify; PyPI-only third parties do not.

Read first, judge second:
- The turn's diff is rendered above inside <diff>; the flagged prose is in <context>.
  Read enough of the transcript and the changed files to know (1) what the user asked
  for, (2) what shipped, and (3) whether any accommodation in the diff exists because
  a first-party dependency lacks or breaks something.
- Run `ccx repo locate` on the dependency in question when ownership is unclear.

Discriminator: Does the diff leave the consumer holding code whose only reason to
exist is a first-party dependency's gap — a re-parse/round-trip dodging a missing
constructor, a subclass or wrapper shimming a missing accessor, a re-implementation of
logic the dependency should own, version-sniffing around its behavior? Landing that
without fixing (or getting the user's agreement to plan the fix in) the dependency
means block=true. Fixing the dependency, adopting its fix, or a justified local
accommodation means block=false.

Workaround tells (lean block=true): the diff adds consumer code whose comment or shape
names a first-party gap ("X isn't constructible", "until X ships", "X doesn't
expose"); a synthesize-and-reparse round-trip standing in for a missing dependency
constructor; a local copy of a computation the dependency owns; prose declaring the
upstream fix out of reach because it "requires a release" — releases are tag-driven
here, that is work to plan, not a blocker.

Legitimate signs (lean block=false): the turn fixes the dependency and adopts it, or
explicitly plans the upstream change with the user's agreement; the accommodation
targets a third-party library with no sibling repo; intentional graceful degradation
the consumer owns (a fallback when an optional binary is absent); the user explicitly
approved the local workaround this turn; the workaround is being deleted, not added.

Do NOT fire when: the diff carries no accommodation code and only the prose mentions
workarounds (reporting or reviewing someone else's is not landing one); the turn ends
by asking the user how to proceed instead of shipping the shim; or the change is in
the dependency's own repo.

When uncertain, return block=false. A missed workaround costs a later cleanup; a false
block on honest work teaches the agent to ignore this gate. Fire only when a specific
tell is clearly present and no do-not-fire condition applies. Put your reasoning
(under 60 words, naming the dependency and the primitive it should grow) in
`reasoning`.""",
    message=(
        "This turn lands a consumer-side workaround for a first-party dependency instead of "
        "fixing the dependency. Why: {reasoning} "
        "The dependency is a sibling repo with tag-driven releases — fix the primitive "
        "there: add the missing surface upstream, bump the pin, and delete the local "
        "accommodation. If a local workaround is genuinely correct, say why in your reply "
        "and proceed."
    ),
    label="upstream_workaround_turn",
    signals=Signals(
        [
            Signal(pattern=WORKAROUND_LEXICON.pattern, weight=1, flags=WORKAROUND_LEXICON.flags),
            Signal(pattern=SIBLING_DEPS.pattern, weight=1, flags=SIBLING_DEPS.flags),
            NlpSignal(
                clauses=[
                    Clause(verb=Phrase("support", "expose", "provide", "offer", "handle"), negated=True),
                    Clause(verb=Phrase("ship", "release", "publish"), tense="prospective"),
                ],
                weight=1,
            ),
        ],
        threshold=2,
        window="turn",
        scope="window",
    ),
    events=Event.Stop,
    diff=True,
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
                                    "cc-transcript views aren't constructible, so we round-trip through "
                                    "the parser for now."
                                ),
                            }
                        ]
                    },
                }
            ]
        ): Block(pattern="first-party"),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "I fixed cc-transcript upstream to add the synthetic event constructor, "
                                    "bumped the pin, and deleted the old round-trip shim."
                                ),
                            }
                        ]
                    },
                }
            ],
            llm={"block": False},
        ): Allow(),
    },
)
