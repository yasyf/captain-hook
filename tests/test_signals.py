from __future__ import annotations

import re
from pathlib import Path

import pytest

from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.state import PrimitiveState
from captain_hook.testing.helpers import fixture_session
from captain_hook.tests.helpers import raw_text
from captain_hook.types import Signal, Signals


def make_ctx(tmp_path: Path | None = None, texts: list[str] | None = None) -> HookContext:
    return HookContext(
        session=SessionStore(tmp_path),
        transcript=fixture_session([raw_text("assistant", t) for t in (texts or [])]),
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
        ],
    )
    def test_window(self, sig: Signals, window: int) -> None:
        assert sig.window == window

    def test_accepts_mixed_signal_types(self) -> None:
        sig = Signals(
            patterns=[Signal(pattern=r"test", weight=1)],
            threshold=1,
        )
        assert len(sig.patterns) == 1


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


class TestScoreSignalsNegativeWeights:
    @pytest.mark.parametrize(
        ("patterns", "text", "expected"),
        [
            pytest.param(
                [Signal(pattern=r"retry", weight=3), Signal(pattern=r"investigating", weight=-2)],
                "retry after investigating",
                1,
                id="negative_weight_subtracts",
            ),
            pytest.param(
                [Signal(pattern=r"retry", weight=1), Signal(pattern=r"evidence", weight=-5)],
                "retry with evidence",
                -4,
                id="score_can_go_below_zero",
            ),
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
        result = ps.match_signals(sig, texts)
        assert result is not None
        assert len(result) == 1

    def test_second_call_returns_none(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue"]
        ps.match_signals(sig, texts)
        result = ps.match_signals(sig, texts)
        assert result is None


class TestMatchSignalsContributing:
    def test_excludes_unrelated_texts(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue", "Unrelated text without keywords"]
        result = ps.match_signals(sig, texts)
        assert result is not None
        assert len(result) == 1
        assert "pre-existing" in result[0]

    def test_multiple_contributing(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"retry", weight=2)], threshold=2, window=10)
        texts = ["retry this", "unrelated", "retry that"]
        result = ps.match_signals(sig, texts)
        assert result is not None
        assert len(result) == 2


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
