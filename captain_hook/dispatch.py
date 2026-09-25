"""Select matching hooks, run their handlers, and translate ``HookResult`` into the Claude Code stdout envelope."""

from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from captain_hook.app import get_hook_candidates
from captain_hook.conditions import matches_conditions
from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.session import SessionStore
from captain_hook.state import HookState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from captain_hook.util import reqenv
from captain_hook.util.caching import once

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from captain_hook.events import BaseHookEvent

ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):"
SYNC_DEADLINE_MARGIN_SECONDS = 5.0
ASYNC_HOOK_TIMEOUT_SECONDS = 180.0
HOOK_FANOUT_THREADS = 64
BACKGROUND_FANOUT_THREADS = 8
OFFLOAD_THREADS = 4

type Envelope = dict[str, Any] | str

_SURFACE_HANDLER_ERRORS: ContextVar[bool] = ContextVar("captain_hook_surface_handler_errors", default=False)


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
        self.abandoned = threading.Event()
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
        self.abandoned.set()
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
def offload_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=OFFLOAD_THREADS, thread_name_prefix="capt-hook-offload")


def run_declarative(spec: HookSpec, evt: BaseHookEvent) -> HookResult | None:
    return (
        HookResult(action=Action.block if spec.block else Action.warn, message=spec.message) if spec.message else None
    )


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
        return entry.handler(evt) if entry.handler else run_declarative(entry.spec, evt)
    except (TranscriptLoadError, EvidenceIncomplete):
        raise
    except Exception as exc:
        if _SURFACE_HANDLER_ERRORS.get():
            raise
        logger.bind(hook=entry.name).exception("hook handler failed")
        faults.record(f"hook {entry.name}", exc, str(evt.cwd) if evt.cwd else None)
        return None


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
) -> HookResult | None:
    from captain_hook.transcripts import release_transcript

    try:
        return _execute_hook(entry, evt, session_dir)
    finally:
        release_transcript(evt.ctx.transcript)


def _execute_hook(
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
) -> HookResult | None:
    """Execute a single registered hook under a reserve-then-release ``max_fires`` protocol.

    Each hook event is a separate ``capt-hook run`` process, so a plain read-check-write of the
    fire count lets batched parallel tool calls all read the pre-increment count and over-fire a
    capped hook. Instead, the count is incremented under an exclusive file lock *before* the
    handler runs (reserve); anything but a delivered truthy result gives the slot back under a
    second lock (release) — a falsy result, an ``Exception``, or an abnormal ``BaseException``
    (``SystemExit``/``KeyboardInterrupt``), which releases and then re-propagates so the abort is
    not silently swallowed. Uncapped hooks (``max_fires is None``) skip the lock entirely.
    """
    hook_session_dir = (session_dir / entry.state_key / (evt.agent_id or "main")) if session_dir else None
    if hook_session_dir:
        hook_session_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(hook_session_dir)

    if entry.spec.max_fires is None:
        if result := run_handler(entry, evt):
            record_fire(entry, evt, result)
        return result

    with store[HookState].mutate() as hook_state:
        if hook_state.fire_count >= entry.spec.max_fires:
            return None
        hook_state.fire_count += 1

    delivered = False
    try:
        if result := run_handler(entry, evt):
            delivered = True
            record_fire(entry, evt, result)
            return result
        return None
    finally:
        if not delivered:
            with store[HookState].mutate() as hook_state:
                hook_state.fire_count -= 1


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
        return None
    try:
        reqenv.checkpoint()
        result = execute_hook(entry, evt, session_dir)
    except reqenv.Abandoned:
        logger.bind(hook=entry.name).info("verdict no longer wanted; hook stopped at a checkpoint")
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
) -> list[Future[HookResult | None]]:
    """Start each group of matching hooks as the worker's fan-out budget admits it, carrying the request's contextvars.

    A group the budget has not admitted by the time the caller's deadline is inside *margin* never
    starts: its hooks are cancelled, which :func:`combine` reads as hooks that were never called.
    """
    from captain_hook.transcripts import release_transcript

    futures: list[Future[HookResult | None]] = [Future() for _ in entries]
    for future, evt in zip(futures, events, strict=True):
        future.add_done_callback(lambda _, transcript=evt.ctx.transcript: release_transcript(transcript))
    blocked = FirstBlock()
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
    submitted: Future[None], group: Sequence[int], futures: Sequence[Future[HookResult | None]], fanout: Fanout,
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
    verdict misses this reply, and its name joins :func:`captain_hook.util.reqenv.abandoned`.

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
            continue
        if not settled(future, margin):
            logger.bind(hook=entry.name).warning("caller deadline reached; abandoning this hook's verdict")
            reqenv.abandoned().append(entry.name)
            continue
        if (result := future.result()) is not None and result.system_message:
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
    evt: BaseHookEvent, *, async_: bool,
) -> tuple[list[RegisteredHook], list[BaseHookEvent]]:
    from captain_hook.transcripts import fork_transcript, release_transcript

    entries = get_hook_candidates(evt, async_=async_)
    forks = []
    try:
        for entry in entries:
            transcript = fork_transcript(evt.ctx.transcript)
            forks.append(replace(evt, ctx=evt.ctx.fork(transcript)))
    except BaseException:
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        raise
    finally:
        release_transcript(evt.ctx.transcript)
    matching = []
    events = []
    try:
        for entry, fork in zip(entries, forks, strict=True):
            if matches_conditions(entry.spec, fork):
                matching.append(entry)
                events.append(fork)
            else:
                release_transcript(fork.ctx.transcript)
    except BaseException:
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        raise
    return matching, events


def dispatch(
    event: Event,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
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
    """
    matching, events = prepare_hook_events(evt, async_=False)
    if not matching:
        return None
    groups = hook_groups(matching)
    fanout = Fanout(len(groups))
    try:
        futures = start_hooks(matching, groups, events, session_dir, SYNC_DEADLINE_MARGIN_SECONDS, fanout)
        return combine(event, matching, futures, SYNC_DEADLINE_MARGIN_SECONDS)
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
                execute_hook(entries[index], events[index], session_dir)
    finally:
        for index in group:
            release_transcript(events[index].ctx.transcript)
