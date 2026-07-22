"""Select matching hooks, run their handlers, and translate ``HookResult`` into the Claude Code stdout envelope."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from captain_hook.app import get_matching_hooks
from captain_hook.session import SessionStore
from captain_hook.state import HookState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):"


def run_declarative(spec: HookSpec, evt: BaseHookEvent) -> HookResult | None:
    return (
        HookResult(action=Action.block if spec.block else Action.warn, message=spec.message) if spec.message else None
    )


def run_handler(entry: RegisteredHook, evt: BaseHookEvent) -> HookResult | None:
    from captain_hook.transcripts import TranscriptLoadError

    try:
        return entry.handler(evt) if entry.handler else run_declarative(entry.spec, evt)
    except TranscriptLoadError:
        raise
    except Exception:
        logger.bind(hook=entry.name).exception("hook handler failed")
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


def format_output(event: Event, result: HookResult) -> dict[str, Any] | None:
    """Render a ``HookResult`` as the JSON envelope Claude Code expects on stdout for *event*."""
    if event in (Event.Stop | Event.SubagentStop):
        return {"decision": "block", "reason": result.message} if result.action is not Action.allow else None
    if event is Event.PermissionRequest:
        return format_permission_decision(result)

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


def dispatch(
    event: Event,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
    *,
    async_: bool = False,
) -> dict[str, Any] | None:
    """Dispatch an event to all matching hooks and combine their results, deny-wins.

    Follows Claude Code's own ``deny > ask > allow`` precedence: a ``block`` from any matching hook
    beats an ``allow``/``rewrite``, so one hook's approval can never short-circuit another hook's
    block. ``warn`` messages registered with ``advisory_on_deny=True`` ride along on the deny — when
    any block fired the result is one block whose message joins the block messages, an advisory
    separator, then the opted-in warn messages (encounter order, ``"\n\n"``-separated). Once a block
    has fired, remaining *handler-backed* hooks are skipped unless they opt into the deny advisory;
    those handlers run regardless of registration order, while the rest avoid doomed API cost and
    ``max_fires`` budget. Message-only declarative hooks still run, but only opted-in warnings join
    the deny. Absent a block, a ``rewrite`` beats a plain ``allow`` — a rewrite *is* an allow carrying
    corrected input, so a broad approval must not drop another hook's rewrite; among rewrites the
    first wins, else the first allow, else the accumulated warns surface alone. Warns are never lost
    to a winner either: they ride along on the winning allow/rewrite as its advisory context
    (``additionalContext``), joined after the rewrite's own note.

    A warn's ``approve`` flag survives the warn-only merge: the rebuilt result carries
    ``any(contributing warns' approve)``, so a context-only merge (every part ``approve=False``,
    e.g. ``evt.context``) stays rider-free while a warn+context merge keeps the ``PreToolUse``
    ``permissionDecision: allow`` rider. The block/allow/rewrite winners carry their own decision.
    """
    matching = [h for h in get_matching_hooks(evt) if h.spec.async_ == async_]

    approval: HookResult | None = None
    rewrite: HookResult | None = None
    blocked = False
    blocks: list[str] = []
    warns: list[str] = []
    deny_advisories: list[str] = []
    warn_approve = False
    for entry in matching:
        if blocked and entry.handler is not None and not entry.spec.advisory_on_deny:
            continue
        match execute_hook(entry, evt, session_dir):
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

    if blocked:
        parts = list(blocks)
        if deny_advisories:
            parts.append(ADVISORY_SEPARATOR)
            parts.extend(deny_advisories)
        return format_output(event, HookResult(action=Action.block, message="\n\n".join(parts) or None))
    if (winner := rewrite or approval) is not None:
        if warns:
            winner = (
                replace(winner, note="\n\n".join(([winner.note] if winner.note else []) + warns))
                if winner.action is Action.rewrite
                else replace(winner, message="\n\n".join(warns))
            )
        return format_output(event, winner)
    if warns:
        return format_output(event, HookResult(action=Action.warn, message="\n\n".join(warns), approve=warn_approve))

    return None
