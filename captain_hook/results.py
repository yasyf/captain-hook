from __future__ import annotations

import json
from concurrent.futures import Future, wait
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Sequence

    from captain_hook.events import BaseHookEvent

ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):"

type Envelope = dict[str, Any] | str


def run_declarative(spec: HookSpec, evt: BaseHookEvent) -> HookResult | None:
    if not spec.message:
        return None
    return HookResult(action=Action.block if spec.block else Action.warn, message=spec.message, confirm=spec.confirm)


def closed_verdict(entry: RegisteredHook, status: str, reason: str) -> HookResult | None:
    """The block a fail-closed hook (``on_incomplete``) renders in place of the verdict it could not reach."""
    if entry.spec.on_incomplete is None:
        return None
    return HookResult(
        action=Action.block,
        message=f"{entry.name} could not judge this call ({status}: {reason}), and it fails closed. "
        f"{entry.spec.on_incomplete}",
    )


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
    compaction's custom instructions, so a non-block result renders as its plain message. The host-fired
    ``ResourcePressure`` takes only the proceed acknowledgement capt-hookd parses: a warn or allow renders it,
    anything else renders nothing.

    Codex fails a hook whose envelope its schema does not name. It takes ``permissionDecision: allow`` only
    beside ``updatedInput`` and reads a ``PostToolUse`` or ``UserPromptSubmit`` block from the top-level
    ``decision``, so under ``CAPT_HOOK_PROVIDER=codex`` a warn or allow carries no approval and those two
    events block the way ``Stop`` does.
    """
    codex = reqenv.provider() == "codex"
    if event is Event.ResourcePressure:
        return {"decision": "proceed"} if result.action in (Action.warn, Action.allow) else None
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
    if codex and event in (Event.PostToolUse | Event.UserPromptSubmit) and result.action is Action.block:
        return {"decision": "block", "reason": result.message}

    approval = {} if codex else {"permissionDecision": "allow"}
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
                    **(approval if event is Event.PreToolUse and result.approve else {}),
                }
            }
        case Action.allow:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event.name,
                    **approval,
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


def envelope_text(envelope: Envelope) -> str:
    return envelope if isinstance(envelope, str) else json.dumps(envelope)
