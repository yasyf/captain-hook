from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import pytest

from captain_hook import T
from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.state import PrimitiveState, text_hash
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Signal, Signals


def make_ctx(
    tmp_path: Path | None = None,
    texts: list[str] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(tmp_path),
        transcript=fixture_session(messages if messages is not None else [T.assistant(t) for t in (texts or [])]),
        settings=None,
    )


class TestSignalDataclass:
    def test_defaults(self) -> None:
        s = Signal(pattern=r"test")
        assert s.weight == 1
        assert s.flags == 0
        assert s.pattern == r"test"

    def test_frozen(self) -> None:
        s = Signal(pattern=r"test")
        with pytest.raises(AttributeError):
            s.weight = 5  # type: ignore[misc]

    def test_custom_values(self) -> None:
        s = Signal(pattern=r"retry", weight=3, flags=re.IGNORECASE)
        assert s.weight == 3
        assert s.flags == re.IGNORECASE


class TestSignalsBundle:
    @pytest.mark.parametrize(
        ("sig", "window"),
        [
            pytest.param(
                Signals(patterns=[Signal(pattern=r"test", weight=1)], threshold=2),
                15,
                id="default_window",
            ),
            pytest.param(
                Signals(patterns=[Signal(pattern=r"test", weight=1)], threshold=3, window=10),
                10,
                id="custom_window",
            ),
            pytest.param(
                Signals(patterns=[Signal(pattern=r"test", weight=1)], threshold=3, window="turn"),
                "turn",
                id="turn_window",
            ),
        ],
    )
    def test_window(self, sig: Signals, window: int | str) -> None:
        assert sig.window == window

    def test_accepts_mixed_signal_types(self) -> None:
        sig = Signals(
            patterns=[Signal(pattern=r"test", weight=1)],
            threshold=1,
        )
        assert len(sig.patterns) == 1

    def test_vetoes_default_empty(self) -> None:
        sig = Signals(patterns=[Signal(pattern=r"test", weight=1)], threshold=1)
        assert sig.vetoes == ()

    def test_origin_defaults_to_assistant(self) -> None:
        assert Signals(patterns=[Signal(pattern=r"x", weight=1)], threshold=1).origin == "assistant"

    def test_origin_accepts_any(self) -> None:
        assert Signals(patterns=[Signal(pattern=r"x", weight=1)], threshold=1, origin="any").origin == "any"

    def test_origin_is_keyword_only(self) -> None:
        import dataclasses

        assert next(f for f in dataclasses.fields(Signals) if f.name == "origin").kw_only is True

    def test_origin_is_frozen(self) -> None:
        sig = Signals(patterns=[Signal(pattern=r"x", weight=1)], threshold=1)
        with pytest.raises(AttributeError):
            sig.origin = "any"  # type: ignore[misc]

    def test_accepts_vetoes(self) -> None:
        sig = Signals(
            patterns=[Signal(pattern=r"test", weight=1)],
            threshold=1,
            vetoes=[Signal(pattern=r"actually a bug")],
        )
        assert len(sig.vetoes) == 1

    @pytest.mark.parametrize("bad_weight", [0, -1, -3])
    def test_nonpositive_pattern_weight_raises(self, bad_weight: int) -> None:
        with pytest.raises(ValueError, match="weight must be positive"):
            Signals(patterns=[Signal(pattern=r"x", weight=bad_weight)], threshold=1)

    @pytest.mark.parametrize("veto_weight", [2, -1])
    def test_nondefault_veto_weight_raises(self, veto_weight: int) -> None:
        with pytest.raises(ValueError, match="weight is meaningless"):
            Signals(
                patterns=[Signal(pattern=r"x", weight=1)],
                threshold=1,
                vetoes=[Signal(pattern=r"y", weight=veto_weight)],
            )

    @pytest.mark.parametrize("bad_threshold", [0, -1])
    def test_nonpositive_threshold_raises(self, bad_threshold: int) -> None:
        with pytest.raises(ValueError, match="threshold must be at least 1"):
            Signals(patterns=[Signal(pattern=r"x", weight=1)], threshold=bad_threshold)


class TestScoreSignals:
    @pytest.mark.parametrize(
        ("patterns", "text", "expected"),
        [
            pytest.param([Signal(pattern=r"retry", weight=2)], "let me retry this", 2, id="single_match"),
            pytest.param([Signal(pattern=r"retry", weight=2)], "everything works fine", 0, id="no_match"),
            pytest.param(
                [Signal(pattern=r"retry", weight=2), Signal(pattern=r"again", weight=1)],
                "let me retry again",
                3,
                id="multiple_matches_sum",
            ),
            pytest.param(
                [Signal(pattern=r"retry", weight=2), Signal(pattern=r"impossible", weight=3)],
                "let me retry",
                2,
                id="partial_match",
            ),
        ],
    )
    def test_score(self, patterns: list[Signal], text: str, expected: int) -> None:
        from captain_hook.signals import score_signals

        assert score_signals(patterns, text) == expected


class TestScoreSignalsFlags:
    @pytest.mark.parametrize(
        ("patterns", "text", "expected"),
        [
            pytest.param(
                [Signal(pattern=r"RETRY", weight=2, flags=re.IGNORECASE)],
                "let me retry",
                2,
                id="case_insensitive_flag",
            ),
            pytest.param([Signal(pattern=r"RETRY", weight=2)], "let me retry", 0, id="no_flag_case_sensitive"),
        ],
    )
    def test_score(self, patterns: list[Signal], text: str, expected: int) -> None:
        from captain_hook.signals import score_signals

        assert score_signals(patterns, text) == expected


class TestScoreSignalsMixed:
    def test_regex_only(self) -> None:
        from captain_hook.signals import score_signals

        patterns = [
            Signal(pattern=r"retry", weight=2),
            Signal(pattern=r"again", weight=1),
        ]
        assert score_signals(patterns, "retry again") == 3


class TestExtractSignalContext:
    def test_returns_matching_lines_for_regex(self) -> None:
        from captain_hook.signals import extract_signal_context

        patterns = [Signal(pattern=r"retry", weight=1)]
        text = "line one\nlet me retry\nline three\nretry again"
        result = extract_signal_context(patterns, text)
        assert len(result) == 2
        assert "retry" in result[0]
        assert "retry" in result[1]

    def test_no_matches_returns_empty(self) -> None:
        from captain_hook.signals import extract_signal_context

        patterns = [Signal(pattern=r"impossible", weight=1)]
        text = "line one\nline two"
        assert extract_signal_context(patterns, text) == []

    def test_respects_flags(self) -> None:
        from captain_hook.signals import extract_signal_context

        patterns = [Signal(pattern=r"RETRY", weight=1, flags=re.IGNORECASE)]
        text = "line one\nlet me retry\nline three"
        result = extract_signal_context(patterns, text)
        assert len(result) == 1


class TestMatchSignalsDedup:
    def test_first_call_returns_matches(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue"]
        result = ps.match_signals(sig, texts, "h")
        assert result is not None
        assert len(result) == 1

    def test_second_call_returns_none(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue"]
        ps.match_signals(sig, texts, "h")
        result = ps.match_signals(sig, texts, "h")
        assert result is None


class TestMatchSignalsContributing:
    def test_excludes_unrelated_texts(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue", "Unrelated text without keywords"]
        result = ps.match_signals(sig, texts, "h")
        assert result is not None
        assert len(result) == 1
        assert "pre-existing" in result[0]

    def test_multiple_contributing(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"retry", weight=2)], threshold=2, window=10)
        texts = ["retry this", "unrelated", "retry that"]
        result = ps.match_signals(sig, texts, "h")
        assert result is not None
        assert len(result) == 2


class TestMatchSignalsAggregation:
    """Presence-union scoring across window entries: cross-entry aggregation, vetoes, per-hook ledger."""

    HOOK = "agg_hook"
    SIG = Signals(
        patterns=[Signal(pattern=r"list", weight=1), Signal(pattern=r"feedback", weight=2)],
        threshold=3,
        scope="window",
    )
    VETO_SIG = Signals(
        patterns=[Signal(pattern=r"list", weight=1), Signal(pattern=r"feedback", weight=2)],
        threshold=3,
        scope="window",
        vetoes=[Signal(pattern=r"you asked")],
    )

    def test_union_of_split_tells_fires(self) -> None:
        ps = PrimitiveState()
        texts = ["a list here", "unrelated", "some feedback"]
        assert ps.match_signals(self.SIG, texts, self.HOOK) == ["a list here", "some feedback"]
        assert ps.consumed == {self.HOOK: {text_hash("a list here"), text_hash("some feedback")}}

    def test_same_signal_across_entries_counts_once(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"list", weight=1)], threshold=2)
        # both entries match only the weight-1 `list` signal: union weight is 1, not 2 → no fire
        assert ps.match_signals(sig, ["first list", "second list"], self.HOOK) is None
        assert ps.consumed == {}

    def test_duplicate_contributor_deduped(self) -> None:
        ps = PrimitiveState()
        # the same text appears twice in the window
        texts = ["a list here", "some feedback", "a list here"]
        assert ps.match_signals(self.SIG, texts, self.HOOK) == ["a list here", "some feedback"]
        assert ps.consumed == {self.HOOK: {text_hash("a list here"), text_hash("some feedback")}}

    def test_veto_in_sibling_entry_suppresses(self) -> None:
        ps = PrimitiveState()
        texts = ["a list here", "some feedback", "you asked for this"]
        assert ps.match_signals(self.VETO_SIG, texts, self.HOOK) is None
        assert ps.consumed == {}

    def test_veto_in_consumed_entry_still_suppresses(self) -> None:
        ps = PrimitiveState()
        ps.consumed_for(self.HOOK).add(text_hash("you asked for this"))
        texts = ["a list here", "some feedback", "you asked for this"]
        # the veto entry is already consumed, but the veto scan covers ALL entries → suppress,
        # and the live candidates that would otherwise fire stay unconsumed
        assert ps.match_signals(self.VETO_SIG, texts, self.HOOK) is None
        assert ps.consumed == {self.HOOK: {text_hash("you asked for this")}}

    def test_spent_contributor_cannot_reaggregate(self) -> None:
        ps = PrimitiveState()
        first = ["a list here", "some feedback"]
        assert ps.match_signals(self.SIG, first, self.HOOK) == ["a list here", "some feedback"]
        # both contributors spent; re-running with them plus one new weak entry does not re-fire
        assert ps.match_signals(self.SIG, ["a list here", "some feedback", "another list"], self.HOOK) is None

    def test_consumed_is_scoped_per_hook(self) -> None:
        # hook A aggregate-fires and consumes its weak contributor; hook B (threshold 1) matching
        # that same weak text still fires — A's consumption must not mute B.
        ps = PrimitiveState()
        weak = "a list here"
        assert ps.match_signals(self.SIG, [weak, "some feedback"], "hook_a") == [weak, "some feedback"]
        sig_b = Signals(patterns=[Signal(pattern=r"list", weight=1)], threshold=1)
        assert ps.match_signals(sig_b, [weak], "hook_b") == [weak]

    def test_user_prompt_submit_aggregate(self) -> None:
        from captain_hook.events import UserPromptSubmitEvent
        from captain_hook.signals import transcript_texts

        ctx = make_ctx(messages=[T.assistant("here is some feedback")])
        evt = UserPromptSubmitEvent(_raw={"prompt": "a list of items"}, ctx=ctx)
        entries = transcript_texts(evt, self.SIG.window)
        assert entries == ["a list of items", "here is some feedback"]
        # the prompt's `list` tell (1) and the prior turn's `feedback` tell (2) sum across the union
        assert PrimitiveState().match_signals(self.SIG, entries, self.HOOK) == [
            "a list of items",
            "here is some feedback",
        ]


class TestMatchSignalsPerText:
    """``scope="text"`` thresholding: a fire needs one candidate text to meet threshold alone."""

    HOOK = "per_text_hook"

    def test_scope_defaults_to_window(self) -> None:
        assert Signals(patterns=[Signal(pattern=r"x", weight=1)], threshold=1).scope == "window"

    def test_split_weak_tells_do_not_fire(self) -> None:
        ps = PrimitiveState()
        sig = Signals(
            patterns=[Signal(pattern=r"alpha", weight=1), Signal(pattern=r"beta", weight=1)],
            threshold=2,
            scope="text",
        )
        assert ps.match_signals(sig, ["alpha only", "beta only"], self.HOOK) is None
        assert ps.consumed == {}

    def test_qualifying_entry_consumes_only_itself(self) -> None:
        ps = PrimitiveState()
        sig = Signals(
            patterns=[Signal(pattern=r"alpha", weight=2), Signal(pattern=r"beta", weight=1)],
            threshold=2,
            scope="text",
        )
        assert ps.match_signals(sig, ["alpha strong", "beta weak"], self.HOOK) == ["alpha strong"]
        assert ps.consumed == {self.HOOK: {text_hash("alpha strong")}}
        # the sub-threshold sibling was never consumed → a threshold-1 sig still fires on it
        sig1 = Signals(patterns=[Signal(pattern=r"beta", weight=1)], threshold=1)
        assert ps.match_signals(sig1, ["beta weak"], self.HOOK) == ["beta weak"]

    def test_two_qualifying_entries_returned_in_window_order(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"alpha", weight=2)], threshold=2, scope="text")
        assert ps.match_signals(sig, ["first alpha", "middle", "second alpha"], self.HOOK) == [
            "first alpha",
            "second alpha",
        ]

    def test_veto_suppresses_even_when_an_entry_qualifies(self) -> None:
        ps = PrimitiveState()
        sig = Signals(
            patterns=[Signal(pattern=r"alpha", weight=2)], threshold=2, scope="text", vetoes=[Signal(pattern=r"nope")]
        )
        assert ps.match_signals(sig, ["alpha strong", "nope veto here"], self.HOOK) is None
        assert ps.consumed == {}


class TestTranscriptTexts:
    def test_returns_window_sized_text_list(self) -> None:
        from captain_hook.events import PostToolUseEvent
        from captain_hook.signals import transcript_texts

        ctx = make_ctx(texts=[f"msg {i}" for i in range(20)])
        evt = PostToolUseEvent(_raw={"tool_name": "Edit"}, ctx=ctx)
        result = transcript_texts(evt, 5)
        assert len(result) <= 5

    def test_returns_user_prompt_for_user_prompt_event(self) -> None:
        from captain_hook.events import UserPromptSubmitEvent
        from captain_hook.signals import transcript_texts

        ctx = make_ctx()
        evt = UserPromptSubmitEvent(_raw={"prompt": "help me debug"}, ctx=ctx)
        result = transcript_texts(evt, 5)
        assert result == ["help me debug"]

    def test_empty_transcript(self) -> None:
        from captain_hook.events import PostToolUseEvent
        from captain_hook.signals import transcript_texts

        ctx = make_ctx(texts=[])
        evt = PostToolUseEvent(_raw={"tool_name": "Edit"}, ctx=ctx)
        result = transcript_texts(evt, 5)
        assert result == []

    def test_filters_empty_text(self) -> None:
        from captain_hook.events import PostToolUseEvent
        from captain_hook.signals import transcript_texts

        ctx = make_ctx(texts=["hello", "", "world"])
        evt = PostToolUseEvent(_raw={"tool_name": "Edit"}, ctx=ctx)
        result = transcript_texts(evt, 10)
        assert "" not in result
        assert len(result) == 2


class TestTranscriptTextsUserPrompt:
    """UserPromptSubmit scans the prior assistant turn, not just the prompt.

    The canonical miss: the assistant dumps options as prose, the user replies "hmm".
    The tells live in the prior ASSISTANT turn — not yet joined by the new prompt in the
    transcript — so the UPS scan must reach back into the recent window. Uses the verbatim
    wall_of_text signals and the f08 writeup already pinned in test_wall_of_text_f08.
    """

    @staticmethod
    def ups_event(messages: list[dict[str, Any]], prompt: str) -> Any:
        from captain_hook.events import UserPromptSubmitEvent

        return UserPromptSubmitEvent(_raw={"prompt": prompt}, ctx=make_ctx(messages=messages))

    def test_prepends_prompt_to_recent_assistant_window(self) -> None:
        from captain_hook.signals import transcript_texts
        from tests.test_wall_of_text_f08 import F08_WRITEUP

        evt = self.ups_event([T.assistant(F08_WRITEUP)], "hmm")
        assert transcript_texts(evt, 6) == ["hmm", F08_WRITEUP]

    def test_assistant_entry_passes_wall_gate(self) -> None:
        from captain_hook.signals import transcript_texts
        from tests.test_wall_of_text_f08 import F08_WRITEUP, WALL_OF_TEXT

        evt = self.ups_event([T.assistant(F08_WRITEUP)], "hmm")
        entries = transcript_texts(evt, WALL_OF_TEXT.window)
        triggering = PrimitiveState().match_signals(WALL_OF_TEXT, entries, "h")
        assert triggering == [F08_WRITEUP]

    def test_window_zero_scores_prompt_alone(self) -> None:
        from captain_hook.signals import transcript_texts
        from tests.test_wall_of_text_f08 import F08_WRITEUP

        evt = self.ups_event([T.assistant(F08_WRITEUP)], "hmm")
        assert transcript_texts(evt, 0) == ["hmm"]


class TestTranscriptTextsOrigin:
    """``origin`` filters candidate authorship: ``"assistant"`` (the ``Signals`` default) drops user prose."""

    @staticmethod
    def texts_for(
        messages: list[dict[str, Any]], window: int | Literal["turn"] = 10, origin: Literal["assistant", "any"] = "any"
    ) -> list[str]:
        from captain_hook.events import PostToolUseEvent
        from captain_hook.signals import transcript_texts

        evt = PostToolUseEvent(_raw={"tool_name": "Edit"}, ctx=make_ctx(messages=messages))
        return transcript_texts(evt, window, origin)

    def test_default_keeps_user_and_assistant(self) -> None:
        msgs = [T.user("the user says leave it"), T.assistant("the assistant plan")]
        assert self.texts_for(msgs) == ["the user says leave it", "the assistant plan"]

    def test_assistant_origin_drops_user(self) -> None:
        msgs = [T.user("the user says leave it"), T.assistant("the assistant plan")]
        assert self.texts_for(msgs, origin="assistant") == ["the assistant plan"]

    def test_any_origin_keeps_user(self) -> None:
        msgs = [T.user("the user says leave it"), T.assistant("the assistant plan")]
        assert self.texts_for(msgs, origin="any") == ["the user says leave it", "the assistant plan"]

    def test_agent_injected_dropped_under_both_origins(self) -> None:
        # An agent-injected user banner is dropped under "assistant" (it is a UserEvent) and under
        # "any" (the is_agent_injected conjunct) — the second layer covers origin="any" hooks.
        msgs = [
            T.user('<teammate-message from="A">the relayed plan</teammate-message>'),
            T.assistant("the assistant plan"),
        ]
        assert self.texts_for(msgs, origin="any") == ["the assistant plan"]
        assert self.texts_for(msgs, origin="assistant") == ["the assistant plan"]


class TestTranscriptTextsOriginUserPrompt:
    """The just-submitted prompt is prepended only under ``origin="any"``; ``"assistant"`` excludes it."""

    @staticmethod
    def ups_event(messages: list[dict[str, Any]], prompt: str) -> Any:
        from captain_hook.events import UserPromptSubmitEvent

        return UserPromptSubmitEvent(_raw={"prompt": prompt}, ctx=make_ctx(messages=messages))

    def test_prompt_prepended_under_any(self) -> None:
        from captain_hook.signals import transcript_texts

        evt = self.ups_event([T.assistant("prior plan")], "the new prompt")
        assert transcript_texts(evt, 6, "any") == ["the new prompt", "prior plan"]

    def test_prompt_excluded_under_assistant(self) -> None:
        from captain_hook.signals import transcript_texts

        evt = self.ups_event([T.assistant("prior plan")], "the new prompt")
        assert transcript_texts(evt, 6, "assistant") == ["prior plan"]

    def test_window_zero_assistant_is_inert(self) -> None:
        # window=0 + origin="assistant" leaves a UPS hook nothing to score (no prior events, no
        # prompt prepend) — why the distinct-requests nudge must opt into origin="any".
        from captain_hook.signals import transcript_texts

        evt = self.ups_event([T.assistant("prior plan")], "1. add foo\n2. fix bar")
        assert transcript_texts(evt, 0, "assistant") == []
        assert transcript_texts(evt, 0, "any") == ["1. add foo\n2. fix bar"]


class TestTranscriptTextsProse:
    @staticmethod
    def texts_for(messages: list[dict[str, Any]], window: int | Literal["turn"] = 10) -> list[str]:
        from captain_hook.events import PostToolUseEvent
        from captain_hook.signals import transcript_texts

        return transcript_texts(PostToolUseEvent(_raw={"tool_name": "Edit"}, ctx=make_ctx(messages=messages)), window)

    def test_meta_injected_events_excluded(self) -> None:
        same = "these pre-existing lines are outside the scope"
        assert self.texts_for([T.user(same, isMeta=True)]) == []
        assert self.texts_for([T.user(same, isCompactSummary=True)]) == []
        assert self.texts_for([T.user(same)]) == [same]

    def test_agent_injected_user_events_excluded(self) -> None:
        # A teammate-message relay banner is a UserEvent with is_agent_injected=True: it echoes
        # another agent's prose into this transcript, so it drops even under the default origin="any"
        # (which otherwise keeps user prose). Genuine human prose with the same tells stays.
        tells = "these pre-existing lines are outside the scope"
        banner = f'<teammate-message from="A">{tells}</teammate-message>'
        assert self.texts_for([T.user(banner)]) == []
        assert self.texts_for([T.user(tells)]) == [tells]

    def test_thinking_block_is_own_entry(self) -> None:
        messages = [T.assistant("visible", T.thinking("hidden plan"))]
        assert self.texts_for(messages) == ["visible", "hidden plan"]

    @pytest.mark.parametrize(
        ("name", "payload", "expected"),
        [
            pytest.param(
                "ReportFindings",
                {
                    "findings": [
                        {"file": "a.py", "summary": "Bug in parser", "failure_scenario": "Crash on empty input"},
                        {"file": "b.py", "summary": "Race in writer", "failure_scenario": "Lost update"},
                    ]
                },
                ["Bug in parser Crash on empty input", "Race in writer Lost update"],
                id="report_findings_per_finding",
            ),
            pytest.param(
                "TaskCreate",
                {"subject": "Fix flaky test", "description": "Stabilize the retry loop"},
                ["Fix flaky test Stabilize the retry loop"],
                id="task_create_subject_description",
            ),
            pytest.param(
                "TaskUpdate",
                {"taskId": "1", "description": "Deferred to a follow-up"},
                ["Deferred to a follow-up"],
                id="task_update_partial_fields",
            ),
            pytest.param(
                "TodoWrite",
                {
                    "todos": [
                        {"content": "Add SSE reconnect test", "status": "pending"},
                        {"content": "Wire the backoff cap", "status": "pending"},
                    ]
                },
                ["Add SSE reconnect test", "Wire the backoff cap"],
                id="todo_write_per_todo",
            ),
            pytest.param("Read", {"file_path": "/tmp/x.py"}, [], id="non_prose_tool_ignored"),
        ],
    )
    def test_tool_payload_prose(self, name: str, payload: dict[str, Any], expected: list[str]) -> None:
        assert self.texts_for([T.assistant(T.tool(name, **payload))]) == expected

    def test_turn_window_scopes_to_current_turn(self) -> None:
        messages = [
            T.user("first question"),
            T.assistant("old answer"),
            T.user("second question"),
            T.assistant("new answer"),
        ]
        assert self.texts_for(messages, "turn") == ["second question", "new answer"]
        assert self.texts_for(messages, 10) == ["first question", "old answer", "second question", "new answer"]

    def test_turn_window_out_reaches_int_window(self) -> None:
        messages = [T.assistant("early deferral")] + [
            line for i in range(6) for line in T.tool_turn("Read", file_path=f"/tmp/f{i}.py")
        ]
        assert "early deferral" not in self.texts_for(messages, 10)
        assert self.texts_for(messages, "turn") == ["early deferral"]


class TestCiteMessage:
    def test_appends_triggered_by(self) -> None:
        from captain_hook.signals import cite_message

        sig = Signals(patterns=[Signal(pattern=r"retry", weight=1)], threshold=1, window=5)
        triggering = ["let me retry this"]
        result = cite_message(sig, triggering, "Stop retrying")
        assert "Triggered by:" in result
        assert "retry" in result

    def test_bare_message_when_no_match(self) -> None:
        from captain_hook.signals import cite_message

        sig = Signals(patterns=[Signal(pattern=r"impossible", weight=1)], threshold=1, window=5)
        triggering = ["no matching content here"]
        result = cite_message(sig, triggering, "Some message")
        assert result == "Some message"
        assert "Triggered by:" not in result

    def test_message_preserved(self) -> None:
        from captain_hook.signals import cite_message

        sig = Signals(patterns=[Signal(pattern=r"retry", weight=1)], threshold=1, window=5)
        triggering = ["retry this"]
        result = cite_message(sig, triggering, "Original message")
        assert result.startswith("Original message")


class TestMixedSignalTypes:
    def test_signals_accepts_mixed_signal_and_nlp_signal(self) -> None:
        from captain_hook.signals.nlp import Clause, NlpSignal, Phrase

        mixed = Signals(
            patterns=[
                Signal(pattern=r"retry", weight=2),
                NlpSignal(clauses=[Clause(noun=Phrase("test"), verb=Phrase("run"))], weight=3),
            ],
            threshold=2,
            window=5,
        )
        assert len(mixed.patterns) == 2

    def test_score_signals_with_mixed_types(self) -> None:
        from captain_hook.signals import score_signals
        from captain_hook.signals.nlp import Clause, NlpSignal, Phrase

        patterns = [
            Signal(pattern=r"retry", weight=2),
            NlpSignal(clauses=[Clause(noun=Phrase("test"), verb=Phrase("run"))], weight=3),
        ]
        assert score_signals(patterns, "I will retry") == 2

    def test_resolve_signals_with_mixed_list(self) -> None:
        from captain_hook.signals import resolve_signals
        from captain_hook.signals.nlp import Clause, NlpSignal, Phrase

        mixed = [
            Signal(pattern=r"retry", weight=2),
            NlpSignal(clauses=[Clause(noun=Phrase("test"), verb=Phrase("run"))], weight=1),
        ]
        result = resolve_signals(mixed)
        assert result is not None
        assert result.threshold == 1
        assert len(result.patterns) == 2

    def test_resolve_signals_preserves_signals_object(self) -> None:
        from captain_hook.signals import resolve_signals
        from captain_hook.signals.nlp import Clause, NlpSignal, Phrase

        sig = Signals(
            patterns=[
                Signal(pattern=r"retry", weight=1),
                NlpSignal(clauses=[Clause(noun=Phrase("test"), verb=Phrase("run"))], weight=2),
            ],
            threshold=3,
        )
        assert resolve_signals(sig) is sig
