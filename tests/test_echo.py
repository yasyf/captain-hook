from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from captain_hook.dispatch import dispatch
from captain_hook.state import PrimitiveState
from captain_hook.types import Event, Signal, Signals
from tests.helpers import make_ctx, make_post_tool_event


def register_nudge(
    message: str,
    *,
    signals: list[Signal] | Signals | None = None,
    events: Event | None = None,
    max_fires: int | None = None,
    **kwargs: Any,
) -> None:
    from captain_hook.primitives.nudge import nudge

    nudge(message, signals=signals, events=events, max_fires=max_fires, **kwargs)


class TestContentLemmas:
    def test_extracts_nouns_verbs_adj(self) -> None:
        lemmas = PrimitiveState.content_lemmas("The pre-existing issue was not caused by my changes")
        assert "issue" in lemmas
        assert "change" in lemmas
        assert "cause" in lemmas
        assert "the" not in lemmas
        assert "not" not in lemmas
        assert "was" not in lemmas

    def test_empty_string_returns_empty_set(self) -> None:
        assert PrimitiveState.content_lemmas("") == set()


class TestIsEcho:
    @pytest.mark.parametrize(
        ("ps", "text", "expected"),
        [
            pytest.param(
                PrimitiveState(echo_lemmas={"issue", "pre-existing", "cause", "change", "fix", "codebase", "bug"}),
                "I'll look at the pre-existing issue and fix it",
                True,
                id="detects_overlap",
            ),
            pytest.param(
                PrimitiveState(echo_lemmas={"issue", "pre-existing", "cause", "change", "fix", "codebase", "bug"}),
                "Now let me read the configuration file",
                False,
                id="rejects_unrelated",
            ),
            pytest.param(
                PrimitiveState(),
                "some random text with issue and change words",
                False,
                id="returns_false_when_echo_lemmas_empty",
            ),
        ],
    )
    def test_is_echo(self, ps: PrimitiveState, text: str, expected: bool) -> None:
        assert ps.is_echo(text) is expected


class TestSeedEchoWindow:
    def test_sets_echo_lemmas_and_window_end(self) -> None:
        ps = PrimitiveState()
        ps.seed_echo_window(["This is a pre-existing issue"], "Fix the problem", transcript_len=10)

        assert len(ps.echo_lemmas) > 0
        from captain_hook.state import ECHO_WINDOW

        assert ps.echo_window_end == 10 + ECHO_WINDOW
        assert "issue" in ps.echo_lemmas or "pre" in ps.echo_lemmas


class TestMatchSignals:
    def test_prevents_double_scoring(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue"]

        result1 = ps.match_signals(sig, texts, "h")
        assert result1 is not None

        result2 = ps.match_signals(sig, texts, "h")
        assert result2 is None

    def test_empty_texts_returns_none(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"anything", weight=1)], threshold=1, window=10)
        assert ps.match_signals(sig, [], "h") is None

    def test_returns_triggering_texts(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue", "Some unrelated text"]

        result = ps.match_signals(sig, texts, "h")
        assert result is not None
        assert len(result) == 1
        assert "pre-existing" in result[0]


class TestEchoIntegration:
    def test_echo_response_not_rescored(self, tmp_path: Path) -> None:
        register_nudge(
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[Signal(pattern=r"pre-existing", weight=2)],
                threshold=2,
                window=10,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, texts=["This is a pre-existing issue, not my change."], n_messages=10)
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        ctx2 = make_ctx(
            tmp_path,
            texts=["This is a pre-existing issue, not my change.", "I'll look into the pre-existing issue and fix it."],
            n_messages=12,
        )
        evt2 = make_post_tool_event(ctx=ctx2)
        r2 = dispatch(Event.PostToolUse, evt2, session_dir=tmp_path)
        assert r2 is None

    def test_unrelated_text_still_fires_in_echo_window(self, tmp_path: Path) -> None:
        register_nudge(
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[
                    Signal(pattern=r"pre-existing", weight=2),
                    Signal(pattern=r"outside.*scope", weight=2),
                ],
                threshold=2,
                window=10,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, texts=["This is a pre-existing issue."], n_messages=10)
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        ctx2 = make_ctx(
            tmp_path,
            texts=[
                "This is a pre-existing issue.",
                "I'll look into the pre-existing issue.",
                "That other bug is outside the scope of this task.",
            ],
            n_messages=12,
        )
        evt2 = make_post_tool_event(ctx=ctx2)
        r2 = dispatch(Event.PostToolUse, evt2, session_dir=tmp_path)
        assert r2 is not None

    def test_echo_window_expires(self, tmp_path: Path) -> None:
        register_nudge(
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[Signal(pattern=r"pre-existing", weight=2)],
                threshold=2,
                window=20,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, texts=["This is a pre-existing issue."], n_messages=10)
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        ctx2 = make_ctx(tmp_path, texts=["Actually this other thing is also pre-existing."], n_messages=25)
        evt2 = make_post_tool_event(ctx=ctx2)
        r2 = dispatch(Event.PostToolUse, evt2, session_dir=tmp_path)
        assert r2 is not None


# Regression: Signal-triggered nudge reads/updates PrimitiveState across calls


class TestSignalNudgePrimitiveState:
    def test_signal_nudge_persists_echo_state(self, tmp_path: Path) -> None:
        register_nudge(
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[Signal(pattern=r"pre-existing", weight=2)],
                threshold=2,
                window=10,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, texts=["This is a pre-existing issue."], n_messages=10)
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        state_file = tmp_path / "primitive_state.json"
        assert state_file.exists(), f"PrimitiveState should be persisted. Files: {list(tmp_path.rglob('*'))}"
        ps = PrimitiveState.model_validate_json(state_file.read_text())
        assert ps.last_fired_at == 10
        assert len(ps.echo_lemmas) > 0
        assert ps.echo_window_end > 0
