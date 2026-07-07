"""Pin the signal-scoring behavior behind cc-notes task 2e78f06 (show-pack rubric case f08).

The rubric reported that f08 "passes the gate on paper but produces no verdict". These tests
demonstrate the discriminating fact: for the real f08 transcript the wall_of_text signal gate
DOES pass (the writeup is one entry scoring S2(1)+S3(2)=3, and ``match_signals`` fires), so the
no-verdict originates downstream of the gate (the LLM ``fire`` verdict), not in scoring. The
split-message case pins the shape that per-entry thresholding used to miss and presence-union
aggregation now catches: two entries scoring 1 and 2 separately fire together on the union.
"""

from __future__ import annotations

import re
from pathlib import Path

from captain_hook.context import HookContext
from captain_hook.events import PostToolUseEvent, UserPromptSubmitEvent
from captain_hook.session import SessionStore
from captain_hook.signals import score_signals, transcript_texts
from captain_hook.state import PrimitiveState, text_hash
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Signal, Signals
from tests.helpers import raw_msg

# Verbatim from cc-skills/plugins/show/hooks/wall_of_text.py (signals=Signals(...)). S1..S5.
WALL_OF_TEXT = Signals(
    [
        Signal(pattern=r"(?im)^\s*(?:\*\*)?(?:option|approach|alternative|path)\s*[A-D1-4]\b", weight=2),
        Signal(pattern=r"(?im)^\s*\d+[.)]\s.+\n(?:.*\n){0,2}\s*\d+[.)]\s", weight=1),
        Signal(pattern=r"(?i)\blet me know (?:which|what you think|if (?:this|that) (?:works|looks))\b", weight=2),
        Signal(pattern=r"(?i)\b(?:approve|sign[- ]?off|pick one|choose (?:one|between))\b", weight=1),
        Signal(
            pattern=r"(?i)\b(?:open|view) (?:it|the (?:report|page|file)) (?:at|in)\b"
            r"|\bsaved (?:the )?(?:report|summary|review) to\b",
            weight=2,
        ),
    ],
    threshold=3,
    window=6,
)

# Verbatim assistant writeup from f08_architecture_writeup_feedback/transcript.jsonl.
F08_WRITEUP = (
    "Here's the full event-bus design, laid out end to end:\n\n"
    "1. Producers publish to a per-tenant topic with an idempotency key.\n"
    "2. The broker fans out to durable subscriber queues with at-least-once delivery.\n"
    "3. Consumers ack after commit; a dead-letter queue catches poison messages.\n"
    "4. A compaction job collapses superseded events nightly.\n\n"
    "The tricky part is exactly-once at the consumer, which we get from the idempotency key "
    "plus a dedup table keyed on (tenant, key). Let me know what you think of the shape."
)


def post_tool_event(messages: list[dict[str, object]], tmp_path: Path) -> PostToolUseEvent:
    ctx = HookContext(session=SessionStore(tmp_path), transcript=fixture_session(messages), settings=None)
    return PostToolUseEvent(_raw={"tool_name": "Read", "tool_input": {"file_path": "/x/ARCH.md"}}, ctx=ctx)


def ups_event(messages: list[dict[str, object]], prompt: str, tmp_path: Path) -> UserPromptSubmitEvent:
    ctx = HookContext(session=SessionStore(tmp_path), transcript=fixture_session(messages), settings=None)
    return UserPromptSubmitEvent(_raw={"prompt": prompt}, ctx=ctx)


class TestF08GatePasses:
    def test_writeup_is_one_entry(self, tmp_path: Path) -> None:
        evt = post_tool_event(
            [raw_msg("user", "sketch the event-bus architecture"), raw_msg("assistant", F08_WRITEUP)],
            tmp_path,
        )
        entries = transcript_texts(evt, WALL_OF_TEXT.window)
        assert entries == ["sketch the event-bus architecture", F08_WRITEUP]

    def test_single_entry_scores_three(self, tmp_path: Path) -> None:
        evt = post_tool_event(
            [raw_msg("user", "sketch the event-bus architecture"), raw_msg("assistant", F08_WRITEUP)],
            tmp_path,
        )
        entries = transcript_texts(evt, WALL_OF_TEXT.window)
        scores = [score_signals(WALL_OF_TEXT.patterns, e) for e in entries]
        assert scores == [0, 3]

    def test_per_signal_matches_are_s2_and_s3(self) -> None:
        matched = [i for i, s in enumerate(WALL_OF_TEXT.patterns) if re.search(s.pattern, F08_WRITEUP, s.flags)]
        assert matched == [1, 2]

    def test_match_signals_fires(self, tmp_path: Path) -> None:
        evt = post_tool_event(
            [raw_msg("user", "sketch the event-bus architecture"), raw_msg("assistant", F08_WRITEUP)],
            tmp_path,
        )
        entries = transcript_texts(evt, WALL_OF_TEXT.window)
        triggering = PrimitiveState().match_signals(WALL_OF_TEXT, entries, "h")
        assert triggering == [F08_WRITEUP]


class TestSplitAcrossMessagesAggregates:
    LIST_ONLY = (
        "Here's the design:\n"
        "1. Producers publish to a per-tenant topic.\n"
        "2. The broker fans out to durable queues.\n"
        "3. Consumers ack after commit.\n"
    )
    FEEDBACK_ONLY = "That covers the shape end to end. Let me know what you think of it."

    def test_aggregate_fires_when_tells_split(self, tmp_path: Path) -> None:
        evt = post_tool_event(
            [
                raw_msg("user", "sketch the event-bus architecture"),
                raw_msg("assistant", self.LIST_ONLY),
                raw_msg("assistant", self.FEEDBACK_ONLY),
            ],
            tmp_path,
        )
        entries = transcript_texts(evt, WALL_OF_TEXT.window)
        scores = [score_signals(WALL_OF_TEXT.patterns, e) for e in entries]
        assert scores == [0, 1, 2]
        ps = PrimitiveState()
        assert ps.match_signals(WALL_OF_TEXT, entries, "h") == [self.LIST_ONLY, self.FEEDBACK_ONLY]
        assert ps.consumed == {"h": {text_hash(self.LIST_ONLY), text_hash(self.FEEDBACK_ONLY)}}


class TestF08UserPromptSubmitGatePasses:
    """The f08 writeup sits in the PRIOR assistant turn and the user replies 'hmm' (cc-notes 9285107).

    UserPromptSubmit no longer short-circuits to the bare prompt: the writeup is scanned as its own
    entry and trips the gate. These fail if the short-circuit is reverted (entries collapse to ['hmm'],
    which scores 0 and yields no match).
    """

    def test_prompt_is_prepended_to_prior_writeup(self, tmp_path: Path) -> None:
        evt = ups_event([raw_msg("assistant", F08_WRITEUP)], "hmm", tmp_path)
        assert transcript_texts(evt, WALL_OF_TEXT.window) == ["hmm", F08_WRITEUP]

    def test_gate_passes_on_writeup_entry(self, tmp_path: Path) -> None:
        evt = ups_event([raw_msg("assistant", F08_WRITEUP)], "hmm", tmp_path)
        entries = transcript_texts(evt, WALL_OF_TEXT.window)
        assert [score_signals(WALL_OF_TEXT.patterns, e) for e in entries] == [0, 3]
        assert PrimitiveState().match_signals(WALL_OF_TEXT, entries, "h") == [F08_WRITEUP]
