"""Echo damping (795069f, capt-hook half): verbatim-quote containment, the summed forward
horizon, and the ``is_agent_injected`` relay-banner exclusion.

The behavioral tests fire a signal nudge through ``dispatch`` twice against one session dir so
``PrimitiveState`` (echo lemmas, ``echo_verbatim``, ``last_fired_at``) carries between passes,
exactly as the two ``capt-hook run`` processes of a real session do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from captain_hook import T
from captain_hook.context import HookContext
from captain_hook.events import PostToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.state import PrimitiveState, normalize_ws
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Event, Signal, Signals
from tests.helpers import make_ctx, make_post_tool_event


def register_nudge(message: str, *, signals: Signals, max_fires: int | None = 5, **kwargs: Any) -> None:
    from captain_hook.primitives.nudge import nudge

    nudge(message, signals=signals, events=Event.PostToolUse, max_fires=max_fires, **kwargs)


def banner_ctx(session_dir: Path, messages: list[tuple[str, str]], *, n_pad: int = 0) -> HookContext:
    lines = [T.assistant("") for _ in range(n_pad)] + [
        (T.user(text) if role == "user" else T.assistant(text)) for role, text in messages
    ]
    return HookContext(session=SessionStore(session_dir), transcript=fixture_session(lines), settings=None)


# --- strip_fired_output: whitespace-normalized substring containment, strip-and-score --------


class TestStripFiredOutput:
    SENTENCE = "Leave the codebase better than you found it"

    def test_quoted_warn_with_new_prose_strips_to_scored_remainder(self) -> None:
        ps = PrimitiveState(echo_verbatim=[self.SENTENCE])
        # A quote embedded in surrounding prose, with collapsed/newlined whitespace, is stripped; the
        # >=20-char remainder survives to be scored and no longer contains the seeded sentence.
        remainder = ps.strip_fired_output(f"As the hook said: {self.SENTENCE}. Moving on.")
        assert remainder == "As the hook said: . Moving on."
        assert self.SENTENCE not in remainder

    def test_pure_quote_strips_below_floor_to_empty(self) -> None:
        ps = PrimitiveState(echo_verbatim=[self.SENTENCE])
        # Nothing survives past the >=20-char floor, so it returns "" and callers drop it (pure quote).
        assert ps.strip_fired_output("Leave   the\n codebase  better than\tyou found it here") == ""

    def test_word_overlap_without_full_substring_is_untouched(self) -> None:
        ps = PrimitiveState(echo_verbatim=[self.SENTENCE])
        # Shares "codebase"/"found"/"leave" but never the full sentence substring: returned verbatim.
        text = "I found the codebase; leave it as is."
        assert ps.strip_fired_output(text) == text

    def test_empty_ledger_leaves_text_untouched(self) -> None:
        text = "Leave the codebase better than you found it"
        assert PrimitiveState().strip_fired_output(text) == text


# --- seed_echo_verbatim: message sentences only, >=20 char floor, FIFO cap -------------


class TestSeedEchoVerbatim:
    MESSAGE = (
        "You appear to be dismissing a pre-existing issue rather than fixing it. "
        "Leave the codebase better than you found it. "
        "Fix it."  # 7 chars -> below the 20-char floor, dropped
    )

    def test_seeds_only_sentences_over_the_floor(self) -> None:
        ps = PrimitiveState()
        ps.seed_echo_verbatim(self.MESSAGE)
        assert "You appear to be dismissing a pre-existing issue rather than fixing it." in ps.echo_verbatim
        assert "Leave the codebase better than you found it." in ps.echo_verbatim
        assert all(len(s) >= 20 for s in ps.echo_verbatim)
        assert "Fix it." not in ps.echo_verbatim

    def test_does_not_seed_matched_excerpts(self) -> None:
        # Seeding excerpts (the transcript tells) would permanently mute the hook: only the
        # message text is ever seeded, never the triggering excerpt.
        ps = PrimitiveState()
        ps.seed_echo_verbatim(self.MESSAGE)
        assert not any("dismissing" in s and "pre-existing" in s and "fixing" in s for s in ps.echo_verbatim) or all(
            s.startswith("You appear") for s in ps.echo_verbatim if "dismissing" in s
        )
        # The excerpt phrase never appears verbatim unless it was in the message itself.
        excerpt = "Pre-existing, not caused by my changes."
        assert ps.strip_fired_output(excerpt) == excerpt

    def test_fifo_cap_evicts_oldest(self) -> None:
        ps = PrimitiveState(echo_verbatim=[f"old durable reminder sentence number {i:02d}" for i in range(39)])
        ps.seed_echo_verbatim("This is a brand new reminder sentence here. This is a second reminder sentence here.")
        assert len(ps.echo_verbatim) == 40
        assert "old durable reminder sentence number 00" not in ps.echo_verbatim
        assert "This is a brand new reminder sentence here." in ps.echo_verbatim
        assert "This is a second reminder sentence here." in ps.echo_verbatim

    def test_reseed_dedupes(self) -> None:
        ps = PrimitiveState()
        ps.seed_echo_verbatim("Leave the codebase better than you found it here.")
        ps.seed_echo_verbatim("Leave the codebase better than you found it here.")
        assert ps.echo_verbatim.count("Leave the codebase better than you found it here.") == 1

    def test_dedupes_within_one_batch(self) -> None:
        # A warn that repeats one sentence many times must seed it once, not flood the FIFO and
        # evict distinct entries: the comprehension dedupes within the batch, not only against
        # the existing ledger.
        ps = PrimitiveState(echo_verbatim=["A distinct earlier reminder sentence here."])
        ps.seed_echo_verbatim("This repeated reminder sentence is long enough. " * 40)
        assert ps.echo_verbatim.count("This repeated reminder sentence is long enough.") == 1
        assert "A distinct earlier reminder sentence here." in ps.echo_verbatim


class TestEchoVerbatimRoundTrip:
    def test_persists_across_serialization(self) -> None:
        ps = PrimitiveState(echo_verbatim=["Leave the codebase better than you found it here."])
        restored = PrimitiveState.model_validate_json(ps.model_dump_json())
        assert restored.echo_verbatim == ["Leave the codebase better than you found it here."]
        assert normalize_ws("a  b\tc\n d") == "a b c d"


# --- e2e: summed forward horizon damps a lemma-echo past the default 5-event window -----


class TestEchoHorizonGap:
    """A paraphrase at fire+8 with ``window=15`` refires today (forward window is only 5); the
    horizon fix widens forward damping to ECHO_WINDOW + window = 20 so it damps."""

    SIG = Signals(patterns=[Signal(pattern=r"pre-existing", weight=2)], threshold=2, window=15)

    def test_paraphrase_within_summed_horizon_is_damped(self, tmp_path: Path) -> None:
        register_nudge("Fix pre-existing issues.", signals=self.SIG)

        ctx1 = make_ctx(tmp_path, texts=["This is a pre-existing issue in the codebase."], n_messages=10)
        assert dispatch_pt(ctx1, tmp_path) is not None

        # fire+8: beyond the default forward window (5) but inside ECHO_WINDOW + window (20).
        ctx2 = make_ctx(tmp_path, texts=["The pre-existing issue in the codebase is still unresolved."], n_messages=18)
        assert dispatch_pt(ctx2, tmp_path) is None


# --- e2e: session-lifetime verbatim containment damps a quote past the lemma window -----


class TestVerbatimQuoteDamping:
    """A verbatim quote of the warn at fire+30 sits past even the summed lemma horizon (30), so
    only ``echo_verbatim`` containment can damp it."""

    SIG = Signals(patterns=[Signal(pattern=r"flaky", weight=2)], threshold=2, window=15)

    def test_verbatim_quote_far_past_horizon_is_damped(self, tmp_path: Path) -> None:
        register_nudge("Stop dismissing the failing tests as flaky.", signals=self.SIG)

        ctx1 = make_ctx(tmp_path, texts=["The failing tests are just flaky, ignore them."], n_messages=10)
        assert dispatch_pt(ctx1, tmp_path) is not None

        quote = "Reminder from the hook: Stop dismissing the failing tests as flaky. Moving on now."
        ctx2 = make_ctx(tmp_path, texts=[quote], n_messages=40)
        assert dispatch_pt(ctx2, tmp_path) is None


# --- e2e: strip-and-score — a quoted warn + a genuinely new violation fires on the remainder --


class TestMixedEchoMessage:
    """A candidate carrying a seeded warn sentence AND a genuinely new violation must not be dropped
    whole: the seeded sentences are stripped and the remainder is scored, so the new violation still
    fires. A pure quote of the warn (nothing left after stripping) stays damped. Both cases sit at
    fire+30 (past the summed lemma horizon of 30) so only verbatim containment is in play."""

    SIG = Signals(patterns=[Signal(pattern=r"flaky", weight=2)], threshold=2, window=15)

    def test_new_violation_after_quoted_warn_fires(self, tmp_path: Path) -> None:
        register_nudge("Stop dismissing the failing tests as flaky.", signals=self.SIG)

        ctx1 = make_ctx(tmp_path, texts=["The failing tests are just flaky, ignore them."], n_messages=10)
        assert dispatch_pt(ctx1, tmp_path) is not None

        # The seeded warn sentence carries "flaky"; the NEW second sentence also does. Dropping the
        # whole candidate (pre-fix) loses the new violation; stripping the seeded sentence keeps it.
        mixed = (
            "As the reminder said: Stop dismissing the failing tests as flaky. "
            "But the new integration suite is also flaky and needs a real fix."
        )
        ctx2 = make_ctx(tmp_path, texts=[mixed], n_messages=40)
        assert dispatch_pt(ctx2, tmp_path) is not None

    def test_pure_quote_stays_damped(self, tmp_path: Path) -> None:
        register_nudge("Stop dismissing the failing tests as flaky.", signals=self.SIG)

        ctx1 = make_ctx(tmp_path, texts=["The failing tests are just flaky, ignore them."], n_messages=10)
        assert dispatch_pt(ctx1, tmp_path) is not None

        pure = "Reminder: Stop dismissing the failing tests as flaky."
        ctx2 = make_ctx(tmp_path, texts=[pure], n_messages=40)
        assert dispatch_pt(ctx2, tmp_path) is None


# --- e2e: precision — word overlap with the warn that is neither quote nor lemma-echo fires


class TestAllowSidePrecision:
    SIG = Signals(patterns=[Signal(pattern=r"flaky", weight=2)], threshold=2, window=15)

    def test_shared_word_deflection_still_fires(self, tmp_path: Path) -> None:
        register_nudge("Stop dismissing the failing tests as flaky.", signals=self.SIG)

        ctx1 = make_ctx(tmp_path, texts=["The failing tests are just flaky, ignore them."], n_messages=10)
        assert dispatch_pt(ctx1, tmp_path) is not None

        # Genuinely new: contains "flaky" (meets threshold) and shares a word or two with the warn,
        # but no >=20-char message-sentence substring and too little lemma overlap to be an echo.
        fresh = "The flaky network mock needs a longer timeout and a retry budget."
        ctx2 = make_ctx(tmp_path, texts=[fresh], n_messages=13)
        assert dispatch_pt(ctx2, tmp_path) is not None


# --- e2e: relay banner leaks into an origin="any" hook today, excluded after ------------


class TestRelayBannerE2E:
    TELLS = "This work is pre-existing and clearly outside the scope of my task."
    SIG_ANY = Signals(
        patterns=[Signal(pattern=r"pre-existing", weight=1), Signal(pattern=r"outside.*scope", weight=1)],
        threshold=2,
        window=15,
        origin="any",
    )

    def banner_line(self) -> str:
        return f'<teammate-message from="A">{self.TELLS}</teammate-message>'

    def test_origin_any_hook_leaks_on_banner_today_excluded_after(self, tmp_path: Path) -> None:
        register_nudge("Do not defer scoped work.", signals=self.SIG_ANY)
        ctx = banner_ctx(tmp_path, [("user", self.banner_line())], n_pad=6)
        assert dispatch_pt(ctx, tmp_path) is None

    def test_origin_any_hook_still_fires_on_genuine_user_prose(self, tmp_path: Path) -> None:
        # Precision: the conjunct excludes only agent-injected user events, not real human prose.
        register_nudge("Do not defer scoped work.", signals=self.SIG_ANY)
        ctx = banner_ctx(tmp_path, [("user", self.TELLS)], n_pad=6)
        assert dispatch_pt(ctx, tmp_path) is not None

    def test_stewardship_pack_hook_does_not_fire_on_relay_banner(self, tmp_path: Path) -> None:
        # Regression guard: the stance stewardship nudge rides origin="assistant" (B2), so a relay
        # banner is already dropped; the is_agent_injected conjunct is the second layer for
        # origin="any" hooks. Assert the real pack hook stays silent on the banner end-to-end.
        import captain_hook.app as app
        from captain_hook.loader import discover_pack
        from captain_hook.packs import manager

        discover_pack("steering", manager.resolve_builtin("steering").path)
        steward = next(h for h in app._state.hooks if h.name == "steering.steering:nudge_1ebed8c4")
        ctx = banner_ctx(tmp_path, [("user", self.banner_line()), ("assistant", "ok, continuing")], n_pad=6)
        evt = PostToolUseEvent(_raw={"tool_name": "Edit"}, ctx=ctx)
        assert steward.handler(evt) is None


def dispatch_pt(ctx: HookContext, session_dir: Path) -> dict[str, Any] | None:
    from captain_hook.dispatch import dispatch

    return dispatch(Event.PostToolUse, make_post_tool_event(ctx=ctx), session_dir=session_dir)
