from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from captain_hook.session import SessionStore
from captain_hook.state import HookState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

if TYPE_CHECKING:
    from captain_hook.app import HookApp
    from captain_hook.events import BaseHookEvent


def run_declarative(spec: HookSpec, evt: BaseHookEvent) -> HookResult | None:
    """Execute a declarative hook spec, returning a block/warn result from its static message.

    Args:
        spec: The hook specification with ``message`` and ``block`` fields.
        evt: The hook event (unused for declarative hooks).

    Returns:
        HookResult with the spec's message, or None if no message is set.
    """
    return (
        HookResult(action=Action.block if spec.block else Action.warn, message=spec.message) if spec.message else None
    )


def execute_hook(
    entry: RegisteredHook,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
) -> HookResult | None:
    """Execute a single registered hook, respecting ``max_fires`` and persisting fire count.

    Args:
        entry: The registered hook to execute.
        evt: The hook event to pass to the handler.
        session_dir: Session directory for state persistence.

    Returns:
        HookResult from the handler, or None if skipped/failed.
    """
    hook_session_dir = (session_dir / entry.name) if session_dir else None
    if hook_session_dir:
        hook_session_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(hook_session_dir)
    hook_state = store[HookState].get() or HookState()

    if entry.spec.max_fires is not None and hook_state.fire_count >= entry.spec.max_fires:
        return None

    try:
        result = entry.handler(evt) if entry.handler else run_declarative(entry.spec, evt)
    except Exception:
        return None

    if result:
        hook_state.fire_count += 1
        store[HookState].set(hook_state)

    return result


def format_output(event: Event, result: HookResult) -> dict[str, Any] | None:
    """Convert a HookResult into the JSON output format expected by Claude Code.

    Args:
        event: The event type that triggered the hook.
        result: The hook result to format.

    Returns:
        JSON-serializable dict, or None for allow results on Stop events.
    """
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
    app: HookApp,
    event: Event,
    evt: BaseHookEvent,
    session_dir: Path | None = None,
    *,
    async_: bool = False,
) -> dict[str, Any] | None:
    """Run all matching hooks for an event and return the formatted output.

    Hooks are evaluated in registration order. A block/allow result short-circuits;
    warnings are accumulated and merged.

    Args:
        app: The HookApp containing registered hooks.
        event: The event type being dispatched.
        evt: The parsed hook event.
        session_dir: Session directory for state persistence.
        async_: If True, only runs hooks registered with ``async_=True``.

    Returns:
        JSON-serializable output dict, or None if no hooks matched/fired.
    """
    matching = [h for h in app.get_matching_hooks(evt) if h.spec.async_ == async_]

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
