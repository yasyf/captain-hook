from __future__ import annotations

import re

from captain_hook import (
    Allow,
    Clause,
    Event,
    Input,
    NlpSignal,
    Phrase,
    Signal,
    Signals,
    T,
    Warn,
    llm_nudge,
)

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


llm_nudge(
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
means fire=true. Fixing the dependency, adopting its fix, or a justified local
accommodation means fire=false.

Workaround tells (lean fire=true): the diff adds consumer code whose comment or shape
names a first-party gap ("X isn't constructible", "until X ships", "X doesn't
expose"); a synthesize-and-reparse round-trip standing in for a missing dependency
constructor; a local copy of a computation the dependency owns; prose declaring the
upstream fix out of reach because it "requires a release" — releases are tag-driven
here, that is work to plan, not a blocker.

Legitimate signs (lean fire=false): the turn fixes the dependency and adopts it, or
explicitly plans the upstream change with the user's agreement; the accommodation
targets a third-party library with no sibling repo; intentional graceful degradation
the consumer owns (a fallback when an optional binary is absent); the user explicitly
approved the local workaround this turn; the workaround is being deleted, not added.

Do NOT fire when: the diff carries no accommodation code and only the prose mentions
workarounds (reporting or reviewing someone else's is not landing one); the turn ends
by asking the user how to proceed instead of shipping the shim; or the change is in
the dependency's own repo.

When uncertain, return fire=false. A missed workaround costs a later cleanup; a false
warning on honest work teaches the agent to ignore this nudge. Fire only when a specific
tell is clearly present and no do-not-fire condition applies. Put your reasoning
(under 60 words, naming the dependency and the primitive it should grow) in
`reasoning`.""",
    message=(
        "This turn lands a consumer-side workaround for a first-party dependency instead of fixing it. "
        "Add the missing surface in the dependency's own repo, bump the pin, and delete the local accommodation."
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
    max_fires=1,
    diff=True,
    tests={
        Input(
            transcript=[
                T.assistant("cc-transcript views aren't constructible, so we round-trip through the parser for now.")
            ]
        ): Warn(pattern="first-party"),
        Input(
            transcript=[
                T.assistant(
                    "I fixed cc-transcript upstream to add the synthetic event constructor, "
                    "bumped the pin, and deleted the old round-trip shim."
                )
            ],
            llm={"fire": False},
        ): Allow(),
    },
)
