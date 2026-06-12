"""Select matching hooks, run their handlers, and translate ``HookResult`` into the Claude Code stdout envelope."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from captain_hook.app import get_matching_hooks
from captain_hook.decisions import record_decision
from captain_hook.session import SessionStore
from captain_hook.state import HookState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent


def run_declarative(spec: HookSpec, evt: BaseHookEvent) -> HookResult | None:
    return (
        HookResult(action=Action.block if spec.block else Action.warn, message=spec.message) if spec.message else None
    )


def execute_hook(
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
) -> HookResult | None:
    """Execute a single registered hook, respecting ``max_fires`` and persisting fire count."""
    hook_session_dir = (session_dir / entry.name) if session_dir else None
    if hook_session_dir:
        hook_session_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(hook_session_dir)
    hook_state = store[HookState].get(HookState())

    if entry.spec.max_fires is not None and hook_state.fire_count >= entry.spec.max_fires:
        return None

    try:
        result = entry.handler(evt) if entry.handler else run_declarative(entry.spec, evt)
    except Exception:
        logger.bind(hook=entry.name).exception("hook handler failed")
        return None

    if result:
        hook_state.fire_count += 1
        store[HookState].set(hook_state)
        try:
            record_decision(entry, evt, result)
        except Exception:
            logger.bind(hook=entry.name).exception("decision write failed")

    return result


def format_output(event: Event, result: HookResult) -> dict[str, Any] | None:
    """Render a ``HookResult`` as the JSON envelope Claude Code expects on stdout for *event*."""
    if event in (Event.Stop | Event.SubagentStop):
        return {"decision": "block", "reason": result.message} if result.action is not Action.allow else None

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
                    **({"permissionDecision": "allow"} if event is Event.PreToolUse else {}),
                }
            }
        case Action.allow:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event.name,
                    "permissionDecision": "allow",
                }
            }


def dispatch(
    event: Event,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
    *,
    async_: bool = False,
) -> dict[str, Any] | None:
    """Dispatch an event to all matching hooks and return the combined result."""
    matching = [h for h in get_matching_hooks(evt) if h.spec.async_ == async_]

    warns: list[str] = []
    for entry in matching:
        match execute_hook(entry, evt, session_dir):
            case HookResult(action=Action.block | Action.allow) as r:
                return format_output(event, r)
            case HookResult(action=Action.warn, message=msg) if msg:
                warns.append(msg)
            case _:
                pass

    if warns:
        return format_output(event, HookResult(action=Action.warn, message="\n\n".join(warns)))

    return None
