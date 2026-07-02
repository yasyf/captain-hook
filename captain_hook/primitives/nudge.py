from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final

from captain_hook.app import on
from captain_hook.signals import cite_message, resolve_signals, transcript_texts
from captain_hook.state import EchoTracker, PrimitiveState, fired_this_turn, hook_name, record_fire
from captain_hook.types import (
    Action,
    Event,
    HookResult,
    InlineTests,
    Signal,
    Signals,
    TCondition,
    Waiting,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal

DEFAULT_FIRES: Final = -1
"""Sentinel for ``max_fires``: apply the primitive's own default cap rather than a number.

Resolves to unlimited for a blocking gate (enforcement must keep firing across turns), 3
for a signal-scored nudge, and 1 for a plain nudge. Pass ``max_fires=None`` for explicit
unlimited, or any int for an explicit cap.
"""


def nudge(
    message: str,
    *,
    when: Callable[[BaseHookEvent], bool] | None = None,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    block: bool = False,
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    async_: bool = False,
) -> None:
    """Register a nudge that warns (or blocks) when conditions or signals match.

    ``when`` and ``signals`` compose: signals decide whether the message fires, and ``when``
    is a veto checked first (both must pass). The triggering event defaults to ``Stop |
    SubagentStop`` for a blocking gate, ``PostToolUse`` for a signal-scored nudge, and
    ``PreToolUse`` otherwise; pass ``events=`` to override. Blocking Stop/SubagentStop gates
    additionally skip while :class:`~captain_hook.types.Waiting`, on top of any ``skip_if``.

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
        if when is not None and not when(evt):
            return None

        if sig:
            ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
            tracker = EchoTracker()
            candidates = [t for t in transcript_texts(evt, sig.window) if not tracker.saw(t, evt=evt)]
            if not (triggering := ps.match_signals(sig, candidates)):
                evt.ctx.s[PrimitiveState].set(ps)
                return None
            ps.last_fired_at = len(evt.ctx.t)
            evt.ctx.s[PrimitiveState].set(ps)
            tracker.record(message, triggering=triggering, evt=evt)
            cited = cite_message(sig, triggering, message)
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

    resolved = events or (
        (Event.Stop | Event.SubagentStop) if block else Event.PostToolUse if sig else Event.PreToolUse
    )
    guards_waiting = block and bool(resolved & (Event.Stop | Event.SubagentStop))
    on(
        resolved,
        only_if=only_if,
        skip_if=(Waiting(), *skip_if) if guards_waiting else tuple(skip_if),
        max_fires=(None if block else 3 if sig else 1) if max_fires == DEFAULT_FIRES else max_fires,
        tests=tests,
        async_=async_,
    )(handler)


def gate(
    message: str,
    *,
    when: Callable[[BaseHookEvent], bool] | None = None,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    async_: bool = False,
) -> None:
    """Register a blocking gate — ``nudge(message, block=True, ...)`` with an explicit signature.

    A gate keeps enforcing: it defaults to unlimited fires (once per turn) and skips while the
    session is :class:`~captain_hook.types.Waiting`, additively with any ``skip_if`` given.

    Example:
        >>> gate("Run tests before committing", when=lambda evt: not has_tests(evt))
    """
    nudge(
        message,
        when=when,
        signals=signals,
        only_if=only_if,
        skip_if=skip_if,
        block=True,
        events=events,
        max_fires=max_fires,
        tests=tests,
        async_=async_,
    )
