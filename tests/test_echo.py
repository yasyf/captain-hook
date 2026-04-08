from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from captain_hook.app import HookApp, _current_app
from captain_hook.dispatch import dispatch
from captain_hook.events import PostToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.state import PrimitiveState, text_hash
from captain_hook.types import Event, Signal, Signals


def make_ctx(tmp_path: Path | None = None, transcript_len: int = 10) -> MagicMock:
    ctx = MagicMock()
    ctx.transcript = MagicMock()
    ctx.transcript.__len__ = MagicMock(return_value=transcript_len)
    ctx.transcript.has_skill = MagicMock(return_value=False)
    ctx.transcript.has_read = MagicMock(return_value=False)
    ctx.transcript.has_edit_to = MagicMock(return_value=False)
    ctx.transcript.has_command = MagicMock(return_value=False)
    ctx.transcript.count_tools = MagicMock(return_value=0)
    ctx.t = ctx.transcript
    store = SessionStore(tmp_path)
    ctx.session = store
    ctx.s = store
    turn = MagicMock()
    turn.start_idx = 5
    ctx.turn = turn
    return ctx


def make_post_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def register_nudge(
    app: HookApp,
    message: str,
    *,
    signals: list[Signal] | Signals | None = None,
    events: Event | None = None,
    max_fires: int | None = None,
    **kwargs: Any,
) -> None:
    from captain_hook.primitives.nudge import nudge

    token = _current_app.set(app)
    try:
        nudge(message, signals=signals, events=events, max_fires=max_fires, **kwargs)
    finally:
        _current_app.reset(token)


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-009 — content_lemmas extracts nouns, verbs, adjectives (not stop words)
# ═══════════════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-001 — Echo detection via content lemma overlap
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsEcho:
    def test_detects_overlap(self) -> None:
        ps = PrimitiveState(echo_lemmas={"issue", "pre-existing", "cause", "change", "fix", "codebase", "bug"})
        assert ps.is_echo("I'll look at the pre-existing issue and fix it")

    def test_rejects_unrelated(self) -> None:
        ps = PrimitiveState(echo_lemmas={"issue", "pre-existing", "cause", "change", "fix", "codebase", "bug"})
        assert not ps.is_echo("Now let me read the configuration file")

    def test_returns_false_when_echo_lemmas_empty(self) -> None:
        ps = PrimitiveState()
        assert not ps.is_echo("some random text with issue and change words")


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-013 — text_hash is deterministic
# ═══════════════════════════════════════════════════════════════════════════════


class TestTextHash:
    def test_deterministic(self) -> None:
        assert text_hash("hello") == text_hash("hello")

    def test_sha256_truncated_16_hex(self) -> None:
        h = text_hash("test")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-003 — Echo window seeding after nudge fires
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedEchoWindow:
    def test_sets_echo_lemmas_and_window_end(self) -> None:
        ps = PrimitiveState()
        ps.seed_echo_window(["This is a pre-existing issue"], "Fix the problem", transcript_len=10)

        assert len(ps.echo_lemmas) > 0
        assert ps.echo_window_end == 10 + PrimitiveState().ECHO_WINDOW
        assert "issue" in ps.echo_lemmas or "pre" in ps.echo_lemmas


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-008 — consume_echoes marks echoed texts as consumed
# ═══════════════════════════════════════════════════════════════════════════════


class TestConsumeEchoes:
    def test_marks_echoed_texts_consumed(self) -> None:
        ps = PrimitiveState(
            echo_lemmas={"issue", "pre-existing", "cause", "change", "fix"},
            echo_window_end=20,
        )
        echo_text = "I'll look at the pre-existing issue and fix it"
        ps.consume_echoes([echo_text], transcript_len=10)
        assert text_hash(echo_text) in ps.consumed

    def test_does_not_consume_when_window_expired(self) -> None:
        ps = PrimitiveState(
            echo_lemmas={"issue", "pre-existing", "cause", "change", "fix"},
            echo_window_end=5,
        )
        echo_text = "I'll look at the pre-existing issue and fix it"
        ps.consume_echoes([echo_text], transcript_len=10)
        assert text_hash(echo_text) not in ps.consumed

    def test_does_not_consume_when_no_echo_lemmas(self) -> None:
        ps = PrimitiveState(echo_window_end=20)
        text = "some random text"
        ps.consume_echoes([text], transcript_len=10)
        assert text_hash(text) not in ps.consumed


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-007 — Content hashing prevents double-scoring the same text
# ═══════════════════════════════════════════════════════════════════════════════


class TestMatchSignals:
    def test_prevents_double_scoring(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue"]

        result1 = ps.match_signals(sig, texts)
        assert result1 is not None

        result2 = ps.match_signals(sig, texts)
        assert result2 is None

    def test_empty_texts_returns_none(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"anything", weight=1)], threshold=1, window=10)
        assert ps.match_signals(sig, []) is None

    def test_returns_triggering_texts(self) -> None:
        ps = PrimitiveState()
        sig = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=10)
        texts = ["This is a pre-existing issue", "Some unrelated text"]

        result = ps.match_signals(sig, texts)
        assert result is not None
        assert len(result) == 1
        assert "pre-existing" in result[0]


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-ECHO-004 — Echo suppression prevents re-fire on paraphrased responses
# ═══════════════════════════════════════════════════════════════════════════════


class TestEchoIntegration:
    def test_echo_response_not_rescored(self, tmp_path: Path) -> None:
        app = HookApp()
        register_nudge(
            app,
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[Signal(pattern=r"pre-existing", weight=2)],
                threshold=2,
                window=10,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, transcript_len=10)
        ctx1.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="This is a pre-existing issue, not my change.")],
            )
        )
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(app, Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        ctx2 = make_ctx(tmp_path, transcript_len=12)
        ctx2.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[
                    MagicMock(text="This is a pre-existing issue, not my change."),
                    MagicMock(text="I'll look into the pre-existing issue and fix it."),
                ],
            )
        )
        evt2 = make_post_tool_event(ctx=ctx2)
        r2 = dispatch(app, Event.PostToolUse, evt2, session_dir=tmp_path)
        assert r2 is None

    # ═══════════════════════════════════════════════════════════════════════════
    # VAL-ECHO-005 — Unrelated text still fires within echo window
    # ═══════════════════════════════════════════════════════════════════════════

    def test_unrelated_text_still_fires_in_echo_window(self, tmp_path: Path) -> None:
        app = HookApp()
        register_nudge(
            app,
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

        ctx1 = make_ctx(tmp_path, transcript_len=10)
        ctx1.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="This is a pre-existing issue.")],
            )
        )
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(app, Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        ctx2 = make_ctx(tmp_path, transcript_len=12)
        ctx2.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[
                    MagicMock(text="This is a pre-existing issue."),
                    MagicMock(text="I'll look into the pre-existing issue."),
                    MagicMock(text="That other bug is outside the scope of this task."),
                ],
            )
        )
        evt2 = make_post_tool_event(ctx=ctx2)
        r2 = dispatch(app, Event.PostToolUse, evt2, session_dir=tmp_path)
        assert r2 is not None

    # ═══════════════════════════════════════════════════════════════════════════
    # VAL-ECHO-006 — Echo window expires after ECHO_WINDOW messages
    # ═══════════════════════════════════════════════════════════════════════════

    def test_echo_window_expires(self, tmp_path: Path) -> None:
        app = HookApp()
        register_nudge(
            app,
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[Signal(pattern=r"pre-existing", weight=2)],
                threshold=2,
                window=20,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, transcript_len=10)
        ctx1.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="This is a pre-existing issue.")],
            )
        )
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(app, Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        ctx2 = make_ctx(tmp_path, transcript_len=25)
        ctx2.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="Actually this other thing is also pre-existing.")],
            )
        )
        evt2 = make_post_tool_event(ctx=ctx2)
        r2 = dispatch(app, Event.PostToolUse, evt2, session_dir=tmp_path)
        assert r2 is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: spaCy model loaded lazily (not at import time)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpacyLazyLoading:
    def test_nlp_not_loaded_at_module_scope(self) -> None:
        import importlib

        import captain_hook.state as state_mod

        importlib.reload(state_mod)
        assert not hasattr(state_mod, "NLP") or state_mod.NLP is None, (
            "spaCy model should not be loaded at module scope"
        )

    def test_content_lemmas_loads_nlp_on_first_call(self) -> None:
        result = PrimitiveState.content_lemmas("The quick brown fox jumps over the lazy dog")
        assert isinstance(result, set)
        assert len(result) > 0
        assert "fox" in result or "jump" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: Echo window expiry boundary uses >= (not >)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEchoWindowBoundary:
    def test_window_expired_at_exact_boundary(self) -> None:
        ps = PrimitiveState(
            echo_lemmas={"issue", "pre-existing", "cause", "change", "fix"},
            echo_window_end=10,
        )
        echo_text = "I'll look at the pre-existing issue and fix it"
        ps.consume_echoes([echo_text], transcript_len=10)
        assert text_hash(echo_text) not in ps.consumed

    def test_window_active_just_before_boundary(self) -> None:
        ps = PrimitiveState(
            echo_lemmas={"issue", "pre-existing", "cause", "change", "fix"},
            echo_window_end=10,
        )
        echo_text = "I'll look at the pre-existing issue and fix it"
        ps.consume_echoes([echo_text], transcript_len=9)
        assert text_hash(echo_text) in ps.consumed


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: Signal-triggered nudge reads/updates PrimitiveState across calls
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignalNudgePrimitiveState:
    def test_signal_nudge_persists_echo_state(self, tmp_path: Path) -> None:
        app = HookApp()
        register_nudge(
            app,
            "Fix pre-existing issues.",
            signals=Signals(
                patterns=[Signal(pattern=r"pre-existing", weight=2)],
                threshold=2,
                window=10,
            ),
            events=Event.PostToolUse,
            max_fires=5,
        )

        ctx1 = make_ctx(tmp_path, transcript_len=10)
        ctx1.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="This is a pre-existing issue.")],
            )
        )
        evt1 = make_post_tool_event(ctx=ctx1)
        r1 = dispatch(app, Event.PostToolUse, evt1, session_dir=tmp_path)
        assert r1 is not None

        state_file = tmp_path / "primitive_state.json"
        assert state_file.exists(), f"PrimitiveState should be persisted. Files: {list(tmp_path.rglob('*'))}"
        ps = PrimitiveState.model_validate_json(state_file.read_text())
        assert ps.last_fired_at == 10
        assert len(ps.echo_lemmas) > 0
        assert ps.echo_window_end > 0
