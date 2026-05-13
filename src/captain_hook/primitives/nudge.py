from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from captain_hook.app import on
from captain_hook.signals import cite_message, resolve_signals, transcript_texts
from captain_hook.state import PrimitiveState, fired_this_turn, hook_name, record_fire
from captain_hook.types import (
    Action,
    Event,
    HookResult,
    Signal,
    Signals,
    TCondition,
    TTest,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal


def nudge(
    message: str,
    *,
    when: Callable[[BaseHookEvent], bool] | None = None,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    block: bool = False,
    events: Event | None = None,
    max_fires: int | None = None,
    tests: TTest | None = None,
    async_: bool = False,
) -> None:
    """Register a nudge that warns (or blocks) when conditions or signals match.

    Example:
        >>> nudge("Remember to run tests", only_if=[TouchedFile("**/*.py")])

        With signal scoring:
            >>> nudge("Stop retrying",
            ...       signals=Signals([Signal(r"retry", weight=2)], threshold=2, window=5))
    """
    sig = resolve_signals(signals)

    def handler(evt: BaseHookEvent) -> HookResult | None:
        if block and fired_this_turn(evt):
            return None

        if sig:
            ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
            texts = transcript_texts(evt, sig.window)
            ps.consume_echoes(texts, len(evt.ctx.t))
            if not (triggering := ps.match_signals(sig, texts)):
                evt.ctx.s[PrimitiveState].set(ps)
                return None
            ps.last_fired_at = len(evt.ctx.t)
            ps.seed_echo_window(triggering, message, len(evt.ctx.t))
            evt.ctx.s[PrimitiveState].set(ps)
            cited = cite_message(sig, triggering, message)
        elif when is not None and not when(evt):
            return None
        else:
            cited = message

        if block:
            record_fire(evt)
            return HookResult.of(Action.block, cited)
        return HookResult.of(Action.warn, cited)

    handler.__name__ = handler.__qualname__ = hook_name(
        "gate" if block else "nudge",
        None,
        message,
    )

    on(
        events or ((Event.Stop | Event.SubagentStop) if block else Event.PostToolUse if sig else Event.PreToolUse),
        only_if=only_if,
        skip_if=skip_if,
        max_fires=max_fires if max_fires is not None else (3 if sig else 1),
        tests=tests,
        async_=async_,
    )(handler)


def gate(message: str, **kwargs: Any) -> None:
    """Register a blocking gate — shorthand for ``nudge(message, block=True, ...)``.

    Example:
        >>> gate("Run tests before committing", when=lambda evt: not has_tests(evt))
    """
    nudge(message, block=True, **kwargs)
