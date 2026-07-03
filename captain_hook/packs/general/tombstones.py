from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import (
    COMMENT_TYPES,
    Allow,
    Clause,
    Event,
    FileFixture,
    Input,
    Introduced,
    Phrase,
    Tool,
    Warn,
    is_past_predicate,
    llm_nudge,
)
from captain_hook.signals.nlp import nlp_scan, parse

if TYPE_CHECKING:
    from collections.abc import Set

    from spacy.tokens import Span


def has_no_longer(sent: Span) -> bool:
    toks = list(sent)
    return any(
        a.lower_ == "no" and b.lower_ == "longer" and (i + 2 >= len(toks) or toks[i + 2].lower_ != "than")
        for i, (a, b) in enumerate(itertools.pairwise(toks))
    )


def past_reference_advmod(sent: Span) -> bool:
    return any(
        is_past_predicate(t)
        and any(c.dep_ == "advmod" and c.lemma_.lower() in {"previously", "formerly", "originally"} for c in t.children)
        for t in sent
    )


def used_to_idiom(sent: Span) -> bool:
    return any(t.lemma_.lower() == "use" and any(c.dep_ == "xcomp" for c in t.children) for t in sent)


def is_marker(text: str) -> bool:
    return (m := re.search(r"[A-Za-z]+", text)) is not None and m.group(0).lower() in {"todo", "fixme", "xxx", "hack"}


def is_tombstone(text: str) -> bool:
    return bool(
        nlp_scan(
            [
                Clause(
                    verb=Phrase(
                        "remove",
                        "delete",
                        "drop",
                        "strip",
                        "eliminate",
                        "extract",
                        "move",
                        "relocate",
                        "migrate",
                        "rename",
                        "inline",
                        "consolidate",
                        "merge",
                        "replace",
                        "retire",
                        "deprecate",
                    ),
                    completed=True,
                    subject="no_nominal",
                ),
                Clause(verb=Phrase("be"), adj=Phrase("previously", "formerly", "originally", "here"), completed=True),
            ],
            text,
        )
    ) or any(has_no_longer(s) or past_reference_advmod(s) or used_to_idiom(s) for s in parse(text).sents)


@dataclass(frozen=True, slots=True)
class TombstoneComments(Introduced):
    """Gating context: comments the pending edit newly introduces whose text reads like a tombstone."""

    kind: str | Set[str] | None = COMMENT_TYPES

    def keep(self, text: str) -> bool:
        return not is_marker(text) and is_tombstone(text)


llm_nudge(
    """Decide whether the pending edit introduces a TOMBSTONE comment — a comment that
narrates the edit itself (what was removed, moved, renamed, or used to be here)
instead of documenting the code that remains. Git history already records the
change, so a tombstone is noise to every future reader.

<tombstone_comments> holds the comments this edit newly introduces that a
syntactic scan flagged as suspects; entries may span multiple lines (block
comments arrive whole, delimiters included). <before_edit> and <after_edit>
hold the edit's old and new text for surrounding context.

The test: does the comment still make sense to a reader who never saw this
edit? A comment that describes the current behavior of the remaining code, or
that guides callers of code that still exists, earns its place.

<examples>
<example fire="true">
# removed the retry logic
Narrates the removal; meaningless to a reader who never saw the deleted code.
</example>
<example fire="true">
/* no longer needed */
Left where code was cut — it refers to nothing that remains.
</example>
<example fire="true">
# moved to utils.py
Points at code that is no longer here; git history records the move.
</example>
<example fire="false">
# removed in API v2 — use fetch_v2() instead
Migration guidance for callers of code that still exists.
</example>
<example fire="false">
# TODO: remove after the June migration
A future action on present code, not narration of this edit.
</example>
<example fire="false">
# no retry here: the queue redelivers on nack
Documents the current behavior of the code that remains.
</example>
<example fire="false">
Removed: legacy auth flow
A changelog or docstring "Removed:" section is the right home for change narration.
</example>
</examples>

Set fire=true only when at least one entry in <tombstone_comments> is clearly a
tombstone. When uncertain, set fire=false — a stray tombstone costs little,
while a false alarm teaches the agent to ignore this nudge. Keep reasoning
under 60 words and quote the offending comment verbatim.""",
    message=lambda r: (
        "Tombstone comment: the edit adds a comment describing code that no longer exists. "
        f"{r.reasoning} Delete the comment line itself; do NOT restore the removed code "
        "(git history records it). If it can instead document the behavior of the code "
        "that remains, rewrite it to say that."
    ),
    contexts=[TombstoneComments()],
    events=Event.PreToolUse,
    only_if=[Tool("Edit", "Write", "MultiEdit")],
    agent=False,
    transcript=False,
    tests={
        Input(
            file="src/app.py", old="retry(fetch, attempts=3)\n", content="# removed the retry logic\nfetch()\n"
        ): Warn(pattern="Tombstone"),
        Input(file="web/app.js", old="startPolling();\n", content="// no longer needed\n"): Warn(pattern="Tombstone"),
        Input(file="src/util.py", old="def helper(): ...\n", content="# moved to utils.py\n"): Warn(
            pattern="Tombstone"
        ),
        Input(file="src/app.py", old="a = 1\n", content="a = 2\n"): Allow(),
        Input(
            file="src/app.py", old="# removed the retry logic\nx()\n", content="# removed the retry logic\ny()\n"
        ): Allow(),  # pre-existing comment: not introduced
        Input(
            file="src/queue.py",
            old="pass\n",
            content="# remove the node from the queue before re-linking\nnode.unlink()\n",
        ): Allow(),  # imperative
        Input(
            file="src/cache.py", old="pass\n", content="# TODO: remove after the June migration\ncleanup()\n"
        ): Allow(),  # veto signal
        Input(
            tool="Write",
            file=FileFixture(name="ts_w1.py", content="# no longer needed\nx = 1\n"),
            content="# no longer needed\nx = 2\n",
        ): Allow(),  # Write: comment already on disk
        Input(
            tool="Write",
            file=FileFixture(name="ts_w2.py", content="x = 1\n"),
            content="# deleted the fallback path\nx = 2\n",
        ): Warn(pattern="Tombstone"),
    },
)
