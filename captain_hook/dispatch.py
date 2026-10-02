"""Select matching hooks, run their handlers, and translate ``HookResult`` into the Claude Code stdout envelope."""

from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from copy import copy
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from captain_hook.app import get_hook_candidates, get_mandatory_hooks, registration_ranks, skips_event
from captain_hook.conditions import matches_conditions
from captain_hook.confirm import confirmed
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import EvidenceIncomplete, fails_open
from captain_hook.state import HookState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from captain_hook.util import reqenv
from captain_hook.util.caching import once

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from captain_hook.events import BaseHookEvent

ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):"
SYNC_DEADLINE_MARGIN_SECONDS = 5.0
MANDATORY_HOOK_THREADS = 8
MANDATORY_COLLECT_SLACK_SECONDS = 0.25
ASYNC_HOOK_TIMEOUT_SECONDS = 180.0
HOOK_FANOUT_THREADS = 64
BACKGROUND_FANOUT_THREADS = 8
OFFLOAD_THREADS = 4

type Envelope = dict[str, Any] | str
type Settle = Callable[[HookResult | None], None]

_SURFACE_HANDLER_ERRORS: ContextVar[bool] = ContextVar("captain_hook_surface_handler_errors", default=False)


class MandatoryDeadlinePassed(Exception):
    """A mandatory hook missed its verdict inside the caller's deadline: queued past it, or still running at it."""


@once
def fanout_budget() -> threading.BoundedSemaphore:
    """The worker-wide bound on synchronous hook groups in flight: :data:`HOOK_FANOUT_THREADS` permits.

    The worker serves up to :data:`captain_hook.worker.service.REQUEST_THREADS` events at once, and
    each fans out onto a pool of its own, so without a shared bound a burst multiplies the two.
    """
    return threading.BoundedSemaphore(HOOK_FANOUT_THREADS)


class Fanout:
    """One event's claim on :func:`fanout_budget`, and the flag that stops its hooks once nobody waits on them.

    A group's permit comes back when the group finishes or when the event's dispatch ends,
    whichever is first. The dispatcher returns the rest rather than the hook threads: a hook the
    caller's deadline abandoned keeps its thread until it next reaches a
    :func:`captain_hook.util.reqenv.checkpoint`, and a permit it still held is what every other
    session's events would queue behind. The budget therefore bounds the hooks somebody is waiting
    on, and an abandoned one holds a thread of its own event's pool and nothing shared.
    """

    def __init__(self, groups: int) -> None:
        self.abandoned = reqenv.Cutoff()
        self.pool = ThreadPoolExecutor(max_workers=groups, thread_name_prefix="capt-hook-hook")
        self._budget = fanout_budget()
        self._held = 0
        self._guard = threading.Lock()

    def admit(self, timeout: float | None) -> bool:
        if not self._budget.acquire(timeout=timeout):
            return False
        with self._guard:
            self._held += 1
        return True

    def settle(self) -> None:
        with self._guard:
            if self._held == 0:
                return
            self._held -= 1
        self._budget.release()

    def close(self) -> None:
        self.abandoned.close()
        with self._guard:
            held, self._held = self._held, 0
        for _ in range(held):
            self._budget.release()
        self.pool.shutdown(wait=False, cancel_futures=True)


@once
def background_pool() -> ThreadPoolExecutor:
    """The one process-wide pool every event's ``async_=True`` hooks fan out onto.

    A background hook runs to :data:`ASYNC_HOOK_TIMEOUT_SECONDS`, three minutes, and nothing waits
    on its verdict, so its fan-out is capped for the whole worker rather than per event.
    """
    return ThreadPoolExecutor(max_workers=BACKGROUND_FANOUT_THREADS, thread_name_prefix="capt-hook-async-hook")


@once
def mandatory_pool() -> ThreadPoolExecutor:
    """The one process-wide pool every event's ``mandatory=True`` hooks run on, :data:`MANDATORY_HOOK_THREADS` wide.

    Fixed rather than sized per event, so the thread count never scales with the events in flight;
    a hook still queued or still running when its caller's deadline passes is the event's
    :class:`MandatoryDeadlinePassed` instead of a late completion.
    """
    return ThreadPoolExecutor(max_workers=MANDATORY_HOOK_THREADS, thread_name_prefix="capt-hook-mandatory")


@once
def offload_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=OFFLOAD_THREADS, thread_name_prefix="capt-hook-offload")


def run_declarative(spec: HookSpec, evt: BaseHookEvent) -> HookResult | None:
    if not spec.message:
        return None
    return HookResult(action=Action.block if spec.block else Action.warn, message=spec.message, confirm=spec.confirm)


@contextmanager
def surfacing_handler_errors() -> Iterator[None]:
    """Let a handler's exception propagate out of :func:`run_handler` instead of reading as no result."""
    token = _SURFACE_HANDLER_ERRORS.set(True)
    try:
        yield
    finally:
        _SURFACE_HANDLER_ERRORS.reset(token)


def run_handler(entry: RegisteredHook, evt: BaseHookEvent) -> HookResult | None:
    from captain_hook import faults
    from captain_hook.transcripts import TranscriptLoadError

    try:
        result = entry.handler(evt) if entry.handler else run_declarative(entry.spec, evt)
        if result is not None and (confirm := result.confirm) is not None:
            return confirmed(evt, entry.name, result, confirm)
        return result
    except (TranscriptLoadError, EvidenceIncomplete):
        raise
    except Exception as exc:
        if _SURFACE_HANDLER_ERRORS.get():
            raise
        logger.bind(hook=entry.name).exception("hook handler failed")
        faults.record(f"hook {entry.name}", exc, str(evt.cwd) if evt.cwd else None)
        return None


def note_evidence_gap(entry: RegisteredHook, exc: EvidenceIncomplete) -> None:
    """Record that *entry* was skipped for incomplete evidence, so its siblings still decide the event."""
    reqenv.evidence_gaps().append(f"{entry.name}: {exc.status}: {exc.reason}")


def closed_verdict(entry: RegisteredHook, status: str, reason: str) -> HookResult | None:
    """The block a fail-closed hook (``on_incomplete``) renders in place of the verdict it could not reach."""
    if entry.spec.on_incomplete is None:
        return None
    return HookResult(
        action=Action.block,
        message=f"{entry.name} could not judge this call ({status}: {reason}), and it fails closed. "
        f"{entry.spec.on_incomplete}",
    )


def record_fire(entry: RegisteredHook, evt: BaseHookEvent, result: HookResult) -> None:
    """Record the ledger decision for a hook that fired (the count is reserved under lock upstream)."""
    from captain_hook.decisions import record_decision

    try:
        record_decision(entry, evt, result)
    except Exception:
        logger.bind(hook=entry.name).exception("decision write failed")


def execute_hook(
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
    *,
    settle: Settle | None = None,
) -> HookResult | None:
    from captain_hook.transcripts import release_transcript

    try:
        return _execute_hook(entry, evt, session_dir, settle)
    finally:
        release_transcript(evt.ctx.transcript)


def _execute_hook(
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None,
    settle: Settle | None,
) -> HookResult | None:
    """Execute a single registered hook under a reserve-then-release ``max_fires`` protocol.

    Each hook event is a separate ``capt-hook run`` process, so a plain read-check-write of the
    fire count lets batched parallel tool calls all read the pre-increment count and over-fire a
    capped hook. Instead, the count is incremented under an exclusive file lock *before* the
    handler runs (reserve); anything but a delivered truthy result gives the slot back under a
    second lock (release) — a falsy result, an ``Exception``, or an abnormal ``BaseException``
    (``SystemExit``/``KeyboardInterrupt``), which releases and then re-propagates so the abort is
    not silently swallowed. A truthy result reached once nobody waits on it any more (the caller's
    deadline passed, or dispatch gave up on the event) is not delivered either: :func:`deliver`
    publishes the ledger write and the caller's *settle* under
    :func:`captain_hook.util.reqenv.publish`, which unwinds as
    :class:`captain_hook.util.reqenv.Abandoned` instead once the event closed, so the slot comes
    back the same way and a closure never splits an accepted verdict from its record. Uncapped
    hooks (``max_fires is None``) skip the lock entirely.
    """
    hook_session_dir = (session_dir / entry.state_key / (evt.agent_id or "main")) if session_dir else None
    if hook_session_dir:
        hook_session_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(hook_session_dir)

    if entry.spec.max_fires is None:
        return deliver(entry, evt, run_handler(entry, evt), settle)

    with store[HookState].mutate() as hook_state:
        if hook_state.fire_count >= entry.spec.max_fires:
            return deliver(entry, evt, None, settle)
        hook_state.fire_count += 1

    delivered: HookResult | None = None
    try:
        delivered = deliver(entry, evt, run_handler(entry, evt), settle)
        return delivered
    finally:
        if delivered is None:
            with store[HookState].mutate() as hook_state:
                hook_state.fire_count -= 1


def deliver(
    entry: RegisteredHook, evt: BaseHookEvent, result: HookResult | None, settle: Settle | None
) -> HookResult | None:
    def accept() -> None:
        if result:
            record_fire(entry, evt, result)
        if settle is not None:
            settle(result)

    reqenv.publish(accept)
    return result


class FirstBlock:
    """The registration index of the earliest hook to block, shared across one event's fan-out.

    The *earliest*, not the first to finish: a hook is skipped only when a hook registered ahead of
    it has blocked, so the deny a sequential dispatch would have rendered is the deny that wins even
    when a later hook completes first.
    """

    def __init__(self) -> None:
        self._index: int | None = None
        self._guard = threading.Lock()

    def record(self, index: int) -> None:
        with self._guard:
            self._index = index if self._index is None else min(self._index, index)

    def before(self, index: int) -> bool:
        with self._guard:
            return self._index is not None and self._index < index

    def precede(self, index: int) -> None:
        """Record a block settled before the fan-out that sits ahead of fan-out *index*: it dooms *index* onward."""
        self.record(index - 1)


def doomed_by_block(entry: RegisteredHook, index: int, blocked: FirstBlock) -> bool:
    """Whether an earlier hook's block makes *entry* a wasted call — the deny is set, and it cannot ride along."""
    return blocked.before(index) and entry.handler is not None and not entry.spec.advisory_on_deny


def hook_groups(entries: Sequence[RegisteredHook]) -> list[list[int]]:
    """The indices of *entries*, grouped by the per-hook state key their registrations share.

    Two registrations under one state key share a session directory — one ``max_fires`` counter, one
    ``PrimitiveState`` — and ``execute_hook``'s reserve-then-release protocol reads that counter
    across the whole group, so a sibling running alongside would see a reservation its predecessor
    goes on to release. A group therefore runs in registration order; the groups run concurrently,
    and a distinctly named hook is a group of one.
    """
    groups: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        groups.setdefault(entry.state_key, []).append(index)
    return list(groups.values())


def run_scheduled(
    index: int,
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None,
    margin: float,
    blocked: FirstBlock,
) -> HookResult | None:
    """Run one hook on a pool thread, checking at its own start what a sequential loop checked in turn."""
    if doomed_by_block(entry, index, blocked):
        return None
    if reqenv.deadline_within(margin):
        logger.bind(hook=entry.name).warning("caller deadline is near; skipping this hook")
        result = closed_verdict(entry, "deadline", "the caller's deadline arrived before it started")
    else:
        try:
            reqenv.checkpoint()
            result = execute_hook(entry, evt, session_dir)
        except reqenv.Abandoned:
            logger.bind(hook=entry.name).info("verdict no longer wanted; hook stopped at a checkpoint")
            return None
        except EvidenceIncomplete as exc:
            if not fails_open(exc):
                raise
            if (result := closed_verdict(entry, exc.status, exc.reason)) is None:
                note_evidence_gap(entry, exc)
                return None
    if result is not None and result.action is Action.block:
        blocked.record(index)
    return result


def run_group(
    group: Sequence[int],
    entries: Sequence[RegisteredHook],
    futures: Sequence[Future[HookResult | None]],
    events: Sequence[BaseHookEvent],
    session_dir: Path | None,
    margin: float,
    blocked: FirstBlock,
    fanout: Fanout,
) -> None:
    """Run one state-key group's hooks in registration order, settling each entry's own future.

    A raising hook settles its own future and cancels the rest of its group, the way a sequential
    dispatch left the hooks behind an aborting one unrun.
    """
    try:
        with reqenv.abandonable(fanout.abandoned):
            for position, index in enumerate(group):
                future = futures[index]
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(run_scheduled(index, entries[index], events[index], session_dir, margin, blocked))
                except BaseException as exc:
                    future.set_exception(exc)
                    for later in group[position + 1 :]:
                        futures[later].cancel()
                    return
    finally:
        fanout.settle()


def start_hooks(
    entries: Sequence[RegisteredHook],
    groups: Sequence[Sequence[int]],
    events: Sequence[BaseHookEvent],
    session_dir: Path | None,
    margin: float,
    fanout: Fanout,
    blocked: FirstBlock,
) -> list[Future[HookResult | None]]:
    """Start each group of matching hooks as the worker's fan-out budget admits it, carrying the request's contextvars.

    A group the budget has not admitted by the time the caller's deadline is inside *margin* never
    starts: its hooks are cancelled, which :func:`combine` reads as hooks that were never called.
    *blocked* carries a block settled before the fan-out, so the hooks it dooms never start either.
    """
    from captain_hook.transcripts import release_transcript

    futures: list[Future[HookResult | None]] = [Future() for _ in entries]
    for future, evt in zip(futures, events, strict=True):
        future.add_done_callback(lambda _, transcript=evt.ctx.transcript: release_transcript(transcript))
    for group in groups:
        if not fanout.admit(collect_budget(margin)):
            logger.bind(hooks=[entries[index].name for index in group]).warning(
                "fan-out budget exhausted until the caller deadline; skipping these hooks"
            )
            for index in group:
                futures[index].cancel()
            continue
        submitted = fanout.pool.submit(
            copy_context().run, run_group, group, entries, futures, events, session_dir, margin, blocked, fanout
        )
        submitted.add_done_callback(
            lambda future, group=tuple(group): cancel_unstarted_group(future, group, futures, fanout)
        )
    return futures


def cancel_unstarted_group(
    submitted: Future[None],
    group: Sequence[int],
    futures: Sequence[Future[HookResult | None]],
    fanout: Fanout,
) -> None:
    if submitted.cancelled():
        for index in group:
            futures[index].cancel()
        fanout.settle()


def collect_budget(margin: float) -> float | None:
    """Seconds left to wait on a running hook: the caller's deadline less *margin*, unbounded for the cold CLI."""
    return None if (left := reqenv.seconds_left()) is None else max(0.0, left - margin)


def settled(future: Future[HookResult | None], margin: float) -> bool:
    """Wait for *future* within what is left of the caller's deadline; ``False`` once that budget is spent.

    Recomputed per hook against the live clock, so the whole fold still ends by the caller's
    deadline however the waits stack up.
    """
    if future.done():
        return True
    done, _ = wait([future], timeout=collect_budget(margin))
    return bool(done)


def format_permission_decision(result: HookResult) -> dict[str, Any] | None:
    match result.action:
        case Action.allow:
            decision: dict[str, Any] = {"behavior": "allow"}
        case Action.rewrite:
            decision = {"behavior": "allow", "updatedInput": result.updated_input}
        case Action.block:
            decision = {"behavior": "deny"} | ({"message": result.message} if result.message else {})
        case Action.warn:
            return None
    return {"hookSpecificOutput": {"hookEventName": Event.PermissionRequest.name, "decision": decision}}


def format_output(event: Event, result: HookResult) -> Envelope | None:
    """Render a ``HookResult`` as the stdout Claude Code expects for *event*.

    Every event takes a JSON envelope except ``PreCompact``, whose schema has no
    ``hookSpecificOutput``: Claude Code appends each successful hook's raw trimmed stdout to the
    compaction's custom instructions, so a non-block result renders as its plain message.
    """
    if event in (Event.Stop | Event.SubagentStop):
        return {"decision": "block", "reason": result.message} if result.action is not Action.allow else None
    if event is Event.PreCompact:
        return {"decision": "block", "reason": result.message} if result.action is Action.block else result.message
    if event is Event.PermissionRequest:
        return format_permission_decision(result)
    if event is Event.MessageDisplay:
        return (
            {"hookSpecificOutput": {"hookEventName": event.name, "displayContent": result.message}}
            if result.action is Action.rewrite
            else None
        )

    match result.action:
        case Action.block:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event.name,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": result.message,
                }
            }
        case Action.warn:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event.name,
                    "additionalContext": result.message,
                    **({"permissionDecision": "allow"} if event is Event.PreToolUse and result.approve else {}),
                }
            }
        case Action.allow:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event.name,
                    "permissionDecision": "allow",
                    **({"additionalContext": result.message} if result.message else {}),
                }
            }
        case Action.rewrite:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event.name,
                    "permissionDecision": "allow",
                    "updatedInput": result.updated_input,
                    **({"additionalContext": result.note} if result.note else {}),
                }
            }


def combine(
    event: Event,
    entries: Sequence[RegisteredHook],
    futures: Sequence[Future[HookResult | None]],
    margin: float,
) -> Envelope | None:
    """Fold the running hooks' results into one envelope in registration order, deny-wins.

    The fold drives the waiting: it reaches a hook, decides whether the verdicts so far leave it
    anything to say, and only then waits for it — so a hook a block already suppressed is never
    waited on, and its exception is never raised, exactly as a sequential dispatch never called it.
    A hook whose future is still unsettled when the caller's deadline arrives is abandoned: its
    verdict misses this reply, and its name joins :func:`captain_hook.util.reqenv.abandoned`. A
    fail-closed hook (``on_incomplete``) that never started or never settled blocks instead.

    Follows Claude Code's own ``deny > ask > allow`` precedence: a ``block`` from any matching hook
    beats an ``allow``/``rewrite``, so one hook's approval can never short-circuit another hook's
    block. ``warn`` messages registered with ``advisory_on_deny=True`` ride along on the deny — when
    any block fired the result is one block whose message joins the block messages, an advisory
    separator, then the opted-in warn messages (registration order, ``"\n\n"``-separated). Once a
    block has fired, a later *handler-backed* hook's result is dropped unless it opted into the deny
    advisory, so a block earlier in registration order renders the same envelope however the hooks
    interleaved. Message-only declarative hooks always count, but only opted-in warnings join the
    deny. Absent a block, a ``rewrite`` beats a plain ``allow`` — a rewrite *is* an allow carrying
    corrected input, so a broad approval must not drop another hook's rewrite; among rewrites the
    first wins, else the first allow, else the accumulated warns surface alone. Warns are never lost
    to a winner either: they ride along on the winning allow/rewrite as its advisory context
    (``additionalContext``), joined after the rewrite's own note.

    A warn's ``approve`` flag survives the warn-only merge: the rebuilt result carries
    ``any(contributing warns' approve)``, so a context-only merge (every part ``approve=False``,
    e.g. ``evt.context``) stays rider-free while a warn+context merge keeps the ``PreToolUse``
    ``permissionDecision: allow`` rider. The block/allow/rewrite winners carry their own decision.
    """
    approval: HookResult | None = None
    rewrite: HookResult | None = None
    blocked = False
    blocks: list[str] = []
    warns: list[str] = []
    deny_advisories: list[str] = []
    warn_approve = False
    notices: list[str] = []
    for index, entry in enumerate(entries):
        if blocked and entry.handler is not None and not entry.spec.advisory_on_deny:
            continue
        if (future := futures[index]).cancelled():
            if (result := closed_verdict(entry, "deadline", "the fan-out budget ran out before it started")) is None:
                continue
        elif not settled(future, margin):
            logger.bind(hook=entry.name).warning("caller deadline reached; abandoning this hook's verdict")
            reqenv.abandoned().append(entry.name)
            if (
                result := closed_verdict(entry, "deadline", "the caller's deadline arrived before its verdict")
            ) is None:
                continue
        else:
            result = future.result()
        if result is not None and result.system_message:
            notices.append(result.system_message)
        match result:
            case HookResult(action=Action.block, message=msg):
                blocked = True
                if msg:
                    blocks.append(msg)
            case HookResult(action=Action.rewrite) as r if rewrite is None:
                rewrite = r
            case HookResult(action=Action.allow) as r if approval is None:
                approval = r
            case HookResult(action=Action.warn, message=msg) as r if msg:
                warns.append(msg)
                if entry.spec.advisory_on_deny:
                    deny_advisories.append(msg)
                warn_approve = warn_approve or r.approve
            case _:
                pass

    envelope: Envelope | None = None
    if blocked:
        parts = list(blocks)
        if deny_advisories:
            parts.append(ADVISORY_SEPARATOR)
            parts.extend(deny_advisories)
        envelope = format_output(event, HookResult(action=Action.block, message="\n\n".join(parts) or None))
    elif (winner := rewrite or approval) is not None:
        if warns:
            winner = (
                replace(winner, note="\n\n".join(([winner.note] if winner.note else []) + warns))
                if winner.action is Action.rewrite
                else replace(winner, message="\n\n".join(warns))
            )
        envelope = format_output(event, winner)
    elif warns:
        envelope = format_output(
            event, HookResult(action=Action.warn, message="\n\n".join(warns), approve=warn_approve)
        )
    if not notices or event is Event.PreCompact:
        return envelope
    return (envelope or {}) | {"systemMessage": "\n\n".join(notices)}


def prepare_hook_events(
    evt: BaseHookEvent,
    *,
    async_: bool,
    mandatory: bool | None = None,
    fail_closed: bool | None = None,
) -> tuple[list[RegisteredHook], list[BaseHookEvent]]:
    from captain_hook.transcripts import fork_transcript, release_transcript

    entries = get_hook_candidates(evt, async_=async_, mandatory=mandatory, fail_closed=fail_closed)
    forks: list[BaseHookEvent] = []
    try:
        for entry in entries:
            transcript = fork_transcript(evt.ctx.transcript, entry.spec.transcript_events)
            fork = copy(evt)
            fork.ctx = evt.ctx.fork(transcript)
            fork.__dict__.pop("cmd", None)
            forks.append(fork)
    except BaseException:
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        raise
    finally:
        release_transcript(evt.ctx.transcript)
    matched: dict[int, bool] = {}
    try:
        for index in sorted(range(len(entries)), key=lambda index: entries[index].spec.transcript_events is None):
            try:
                matched[index] = matches_conditions(entries[index].spec, forks[index])
            except EvidenceIncomplete as exc:
                if not fails_open(exc):
                    raise
                if entries[index].spec.on_incomplete is None:
                    note_evidence_gap(entries[index], exc)
                matched[index] = entries[index].spec.on_incomplete is not None
        for index, fork in enumerate(forks):
            if not matched[index]:
                release_transcript(fork.ctx.transcript)
    except BaseException:
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        raise
    kept = [index for index in range(len(entries)) if matched[index]]
    return [entries[index] for index in kept], [forks[index] for index in kept]


def completion_key(entry: RegisteredHook, ordinal: int) -> str:
    return f"{entry.state_key}#{ordinal}"


def mandatory_completions(event: Event) -> dict[str, RegisteredHook]:
    """The completion each of *event*'s ``mandatory=True`` registrations records, keyed per registration.

    Two registrations sharing a state key share one session directory but each owes the event its
    own verdict, so the key carries the registration's place among the event's mandatory hooks and
    the worker can hold every registration to exactly one completion.
    """
    return {completion_key(hook, ordinal): hook for ordinal, hook in enumerate(get_mandatory_hooks(event))}


@dataclass(frozen=True, slots=True)
class MandatoryBound:
    """The absolute deadlines one event's mandatory hooks run under, fixed once when the event starts.

    ``deadline_unix_ms`` is the hooks' own: the caller's deadline less the reply margin when that
    fits, the caller's own once the request is already inside the margin, ``None`` for the cold
    CLI. ``cutoff`` is where :func:`collect_mandatory` stops waiting,
    :data:`MANDATORY_COLLECT_SLACK_SECONDS` past the hooks' deadline and never past the caller's;
    bound as the hooks' abandon signal, a verdict reached after it unwinds at its
    :func:`captain_hook.util.reqenv.checkpoint` instead of recording a completion or keeping a
    fire slot. Both are absolute, so a pause between reading the clock and binding never moves
    either later.
    """

    deadline_unix_ms: int | None
    cutoff: reqenv.Cutoff

    @classmethod
    def of(cls, margin: float) -> MandatoryBound:
        if (ov := reqenv.current()) is None or ov.deadline_unix_ms == 0:
            return cls(None, reqenv.Cutoff())
        deadline = ov.deadline_unix_ms if reqenv.deadline_within(margin) else ov.deadline_unix_ms - int(margin * 1000)
        slack = int(MANDATORY_COLLECT_SLACK_SECONDS * 1000)
        return cls(deadline, reqenv.Cutoff(min(ov.deadline_unix_ms, deadline + slack)))

    def deadline(self) -> AbstractContextManager[None]:
        return nullcontext() if self.deadline_unix_ms is None else reqenv.deadline_at(self.deadline_unix_ms)


def run_mandatory(
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None,
    ordinal: int,
    bound: MandatoryBound,
    future: Future[HookResult | None],
) -> None:
    """Run one mandatory hook under the event's :class:`MandatoryBound`, then publish its completion into *future*.

    The completion and the settlement are one :func:`captain_hook.util.reqenv.publish` with the
    hook's ledger write, under the cutoff's closure lock: a verdict is accepted whole before the
    collector closes the event or refused whole after it, never half-recorded. Incomplete
    transcript evidence leaves the hook unrun, settling the future with no completion; a cutoff
    already passed when the hook starts or closed by the time it publishes, or any other
    exception, is the whole event's and propagates through the future.
    """
    if bound.cutoff.is_set():
        raise MandatoryDeadlinePassed(f"{entry.name}: the caller's deadline passed while the hook was queued")
    key = completion_key(entry, ordinal)

    def settle(result: HookResult | None) -> None:
        reqenv.note_mandatory_completed(key)
        future.set_result(result)

    try:
        try:
            with bound.deadline(), surfacing_handler_errors():
                if skips_event(entry.spec, evt) or not matches_conditions(entry.spec, evt):
                    reqenv.publish(partial(settle, None))
                else:
                    execute_hook(entry, evt, session_dir, settle=settle)
        except EvidenceIncomplete as exc:
            if not fails_open(exc):
                raise
            logger.bind(hook=entry.name, status=exc.status, reason=exc.reason).warning(
                "mandatory hook left unrun: evidence incomplete"
            )
            reqenv.publish(partial(future.set_result, None))
    except reqenv.Abandoned:
        raise MandatoryDeadlinePassed(f"{entry.name}: finished past the caller's deadline") from None


def run_mandatory_group(
    group: Sequence[int],
    entries: Sequence[RegisteredHook],
    futures: Sequence[Future[HookResult | None]],
    events: Sequence[BaseHookEvent],
    session_dir: Path | None,
    bound: MandatoryBound,
) -> None:
    """Run one state-key group's mandatory hooks in registration order, settling each entry's own future.

    Registrations sharing a state key share one ``max_fires`` counter, so they run one after another
    the way :func:`run_group` runs them: side by side, the first could reserve the slot the second
    then skips, and a deny the second would have rendered is lost. A raising hook settles its own
    future and cancels the rest of its group.
    """
    with reqenv.abandonable(bound.cutoff):
        for position, index in enumerate(group):
            future = futures[index]
            if not future.set_running_or_notify_cancel():
                continue
            try:
                run_mandatory(entries[index], events[index], session_dir, index, bound, future)
            except BaseException as exc:
                future.set_exception(exc)
                for later in group[position + 1 :]:
                    futures[later].cancel()
                return


def collect_mandatory(
    entries: Sequence[RegisteredHook], futures: Sequence[Future[HookResult | None]], cutoff: reqenv.Cutoff
) -> None:
    """Wait for every mandatory future until *cutoff*, close it, then conclude the phase from what settled.

    The closure comes first and takes the cutoff's lock, so every publication that beat it is a
    settled future and none can follow it; only then is each future read, and the phase recorded
    in :func:`captain_hook.util.reqenv.mandatory_phase` as settled or failed, exactly once. Past
    the cutoff every hook not yet started is cancelled and every one still running is the
    event's failure: a hook that ignores its budget keeps its thread until it returns, but a
    verdict nobody waited for records no completion and keeps no fire slot. A hook that raised
    dooms the event the same way.
    """
    phase = reqenv.mandatory_phase()
    wait(futures, timeout=cutoff.seconds_left())
    cutoff.close()
    for future in futures:
        future.cancel()
    try:
        for entry, future in zip(entries, futures, strict=True):
            if future.cancelled():
                raise MandatoryDeadlinePassed(f"{entry.name}: left unrun at the caller's deadline")
            if not future.done():
                raise MandatoryDeadlinePassed(f"{entry.name}: still running at the caller's deadline")
            future.result()
    except BaseException:
        phase.conclude("failed")
        raise
    phase.conclude("settled")


def dispatch_mandatory(
    evt: BaseHookEvent,
    session_dir: Path | None = None,
    margin: float = SYNC_DEADLINE_MARGIN_SECONDS,
) -> tuple[list[RegisteredHook], list[Future[HookResult | None]]]:
    """Run the event's ``mandatory=True`` hooks to their verdicts, ahead of every budget, and wait for all of them.

    The Go client denies a guarded call whose mandatory hooks it cannot see complete, so these
    take no fan-out permit and never skip at the deadline margin: each state-key group runs on
    :func:`mandatory_pool`, so a slow one never holds the guard registered after it behind it, and
    each hook records its completion in :func:`captain_hook.util.reqenv.mandatory_completed`
    whether its conditions matched, its own opt-outs (:func:`captain_hook.app.skips_event`) left
    the event alone, or it ran to a verdict. Each hook runs under the event's
    :class:`MandatoryBound`, the caller's deadline less *margin*, so an inner call that honors the
    deadline returns while the reply can still be delivered, and :func:`collect_mandatory` waits
    no longer than that.
    Incomplete transcript evidence records nothing — a mandatory hook is evidence-free by
    contract, so a fail-open skip leaves it unrun — and any other exception, a handler's included,
    is the whole event's: a crashed mandatory hook must read as no completion, never as a verdict.
    The settled futures fold into :func:`combine` at the hooks' own registration positions.
    The worker turns a failed phase into the event's deny only when it recognizes the failure
    and its reply reaches the client; a transport that goes silent is the client's call, which
    retries a timed-out guard once and then warns.
    """
    from captain_hook.transcripts import fork_transcript, release_transcript

    entries = get_mandatory_hooks(evt.event)
    if not entries:
        return [], []
    forks: list[BaseHookEvent] = []
    try:
        for entry in entries:
            fork = copy(evt)
            fork.ctx = evt.ctx.fork(fork_transcript(evt.ctx.transcript, entry.spec.transcript_events))
            fork.__dict__.pop("cmd", None)
            forks.append(fork)
    except BaseException:
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        raise
    futures: list[Future[HookResult | None]] = [Future() for _ in entries]
    for future, fork in zip(futures, forks, strict=True):
        future.add_done_callback(lambda _, transcript=fork.ctx.transcript: release_transcript(transcript))
    bound = MandatoryBound.of(margin)
    pool = mandatory_pool()
    for group in hook_groups(entries):
        pool.submit(copy_context().run, run_mandatory_group, group, entries, futures, forks, session_dir, bound)
    collect_mandatory(entries, futures, bound.cutoff)
    return entries, futures


def in_registration_order(
    entries: Sequence[RegisteredHook], futures: Sequence[Future[HookResult | None]]
) -> tuple[list[RegisteredHook], list[Future[HookResult | None]]]:
    ranks = registration_ranks()
    folded = sorted(zip(entries, futures, strict=True), key=lambda pair: ranks[id(pair[0])])
    return [entry for entry, _ in folded], [future for _, future in folded]


def dispatch(
    event: Event,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
    *,
    advisory: bool = True,
) -> Envelope | None:
    """Dispatch an event to all matching hooks at once and combine their results, deny-wins.

    The event's hooks are independent, so they all start together on the event's own
    :class:`Fanout` and the event costs the slowest hook rather than the sum: five listeners on
    one ``PostToolUse`` no longer serialize behind each other. Only the *starting* is concurrent —
    :func:`combine` folds what comes back in registration order, so the envelope is the one
    sequential dispatch would have rendered whatever order the hooks finished in.

    The caller's deadline bounds both ends: a hook does not start once the deadline is inside
    :data:`SYNC_DEADLINE_MARGIN_SECONDS`, and a hook still running when the budget runs out has its
    verdict abandoned rather than holding the reply. Once the envelope is settled, every hook still
    running — abandoned, or doomed by an earlier block — unwinds at its next
    :func:`captain_hook.util.reqenv.checkpoint`, and whatever never started is cancelled.

    The event's ``mandatory=True`` hooks are the exception to every bound above: they run first,
    to their verdicts, on their own pool (:func:`dispatch_mandatory`), but fold at their own
    registration positions, so a mandatory block dooms exactly the handler-backed hooks registered
    after it and an advisory hook registered before it keeps its run, its ledger write, and its
    claim on the deny's reason. ``advisory=False`` stops after them, so a caller already inside the
    deadline margin still completes the guard while skipping the hooks the margin exists for; a
    fail-closed hook (``on_incomplete``) that matches then blocks, since it never got to judge.
    """
    from captain_hook.transcripts import release_transcript

    try:
        entries, futures = dispatch_mandatory(evt, session_dir)
    except BaseException:
        release_transcript(evt.ctx.transcript)
        raise
    if not advisory:
        closed, forks = prepare_hook_events(evt, async_=False, mandatory=False, fail_closed=True)
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        unstarted: list[Future[HookResult | None]] = [Future() for _ in closed]
        for future in unstarted:
            future.cancel()
        return combine(
            event, *in_registration_order(entries + closed, futures + unstarted), SYNC_DEADLINE_MARGIN_SECONDS
        )
    matching, events = prepare_hook_events(evt, async_=False, mandatory=False)
    if not matching:
        return combine(event, entries, futures, SYNC_DEADLINE_MARGIN_SECONDS)
    ranks = registration_ranks()
    blocked = FirstBlock()
    for entry, future in zip(entries, futures, strict=True):
        if (result := future.result()) is not None and result.action is Action.block:
            blocked.precede(sum(ranks[id(hook)] < ranks[id(entry)] for hook in matching))
            break
    groups = hook_groups(matching)
    fanout = Fanout(len(groups))
    try:
        started = start_hooks(matching, groups, events, session_dir, SYNC_DEADLINE_MARGIN_SECONDS, fanout, blocked)
        return combine(
            event, *in_registration_order(entries + matching, futures + started), SYNC_DEADLINE_MARGIN_SECONDS
        )
    finally:
        fanout.close()


def envelope_text(envelope: Envelope) -> str:
    return envelope if isinstance(envelope, str) else json.dumps(envelope)


def dispatch_async(evt: BaseHookEvent, session_dir: Path | None = None) -> None:
    """Run the event's ``async_=True`` hooks concurrently, each under its own deadline.

    Claude Code never reads an async hook's output, so results are recorded but not rendered.
    Each hook's :data:`ASYNC_HOOK_TIMEOUT_SECONDS` budget runs from its own start, so a slow
    background hook no longer eats the next one's. Their pool is
    :func:`background_pool`, not the one the synchronous fan-out shares: async hooks are the long
    ones, and a session's worth of them would otherwise hold every thread a blocking gate needs.
    """
    entries, events = prepare_hook_events(evt, async_=True)
    pool = background_pool()
    futures = [
        pool.submit(copy_context().run, run_background_group, group, entries, events, session_dir)
        for group in hook_groups(entries)
    ]
    for future in futures:
        future.result()


def run_background_group(
    group: Sequence[int],
    entries: Sequence[RegisteredHook],
    events: Sequence[BaseHookEvent],
    session_dir: Path | None,
) -> None:
    from captain_hook.transcripts import release_transcript

    try:
        for index in group:
            with reqenv.deadline_in(ASYNC_HOOK_TIMEOUT_SECONDS):
                try:
                    execute_hook(entries[index], events[index], session_dir)
                except EvidenceIncomplete as exc:
                    if not fails_open(exc):
                        raise
                    note_evidence_gap(entries[index], exc)
    finally:
        for index in group:
            release_transcript(events[index].ctx.transcript)
