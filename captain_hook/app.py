from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from captain_hook.conditions import matches_conditions
from captain_hook.state import caller_file, hook_name
from captain_hook.types import (
    CustomCondition,
    Event,
    HookSpec,
    InlineTests,
    RegisteredHook,
    TCondition,
    condition_events,
)

if TYPE_CHECKING:
    from cc_transcript.activity import UserClassifier

    from captain_hook.events import BaseHookEvent
    from captain_hook.settings import HooksSettings
    from captain_hook.types import HookResult

HookHandler = Callable[["BaseHookEvent"], "HookResult | None"]

VALID_CONDITION_TYPES = tuple(t for t in get_args(TCondition) if t is not CustomCondition)
VALID_CONDITION_NAMES = ", ".join(t.__name__ for t in VALID_CONDITION_TYPES) + ", or a CustomCondition"


class AsyncDecisionError(TypeError):
    """A hook combined ``async_=True`` with a decision-capable event, whose verdict would be lost."""


def reject_async_decision(events: Event, async_: bool) -> None:
    """Reject an async hook on a decision-capable event: Claude Code never awaits its stdout.

    A background (``async_=True``) hook's output is fire-and-forget, so an allow/deny/block
    verdict it returns on a ``DECISION_EVENTS`` event is silently discarded — the gate never
    runs. Only the synchronous registration can decide those events.
    """
    if not async_:
        return
    from captain_hook.cli import DECISION_EVENTS

    if bad := set(events) & DECISION_EVENTS:
        names = ", ".join(sorted(e.name for e in bad if e.name))
        raise AsyncDecisionError(
            f"async_=True is invalid on decision-capable event(s) {names}: Claude Code never awaits an "
            f"async hook's stdout, so the gate's verdict is silently discarded. Register it synchronously."
        )


def validate_conditions(conditions: Sequence[TCondition], label: str, events: Event | None = None) -> None:
    for c in conditions:
        if not isinstance(c, (*VALID_CONDITION_TYPES, CustomCondition)):
            raise TypeError(
                f"Invalid condition in {label}: {c!r} (type {type(c).__name__}). "
                f"Expected one of: {VALID_CONDITION_NAMES}."
            )
        if events is not None and not (events & (valid := condition_events(c))):
            raise TypeError(
                f"{c!r} in {label} can never match on {events!r} — it reads the current tool input, "
                f"which only exists on {valid!r}."
            )


def validate_handler_signature(fn: HookHandler) -> None:
    sig = inspect.signature(fn)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(params) != 1:
        raise TypeError(
            f"Handler {fn.__name__} has wrong signature: expected (evt) -> HookResult | None, "
            f"got {sig}. Hook handlers must accept exactly one positional parameter (the event)."
        )
    required_kw = [
        p
        for p in sig.parameters.values()
        if p.kind == inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    ]
    if required_kw:
        names = ", ".join(p.name for p in required_kw)
        raise TypeError(
            f"Handler {fn.__name__} has required keyword-only parameter(s): {names}. "
            f"Hook handlers are called as handler(evt) — keyword-only parameters must have defaults."
        )


@dataclass(frozen=True, slots=True)
class LoadError:
    source: str
    exc: BaseException
    pack: str | None = None


@dataclass
class State:
    hooks: list[RegisteredHook] = field(default_factory=list)
    gitignore_patterns: list[str] = field(default_factory=list)
    settings: HooksSettings | None = None
    classifier: UserClassifier | None = None
    load_errors: list[LoadError] = field(default_factory=list)


_GLOBAL_STATE = State()
_STATE_VAR: ContextVar[State] = ContextVar("captain_hook_state")


def current_state() -> State:
    try:
        return _STATE_VAR.get()
    except LookupError:
        return _GLOBAL_STATE


@contextmanager
def use_state(state: State) -> Iterator[State]:
    token = _STATE_VAR.set(state)
    try:
        yield state
    finally:
        _STATE_VAR.reset(token)


class StateProxy:
    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(current_state(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(current_state(), name, value)


_state = StateProxy()


def reset() -> None:
    _state.hooks.clear()
    _state.gitignore_patterns.clear()
    _state.load_errors.clear()
    _state.settings = None
    _state.classifier = None


def load_gitignore(root: Path) -> None:
    _state.gitignore_patterns.clear()
    if not (gitignore := root / ".gitignore").exists():
        return
    _state.gitignore_patterns.extend(
        line.rstrip("/")
        for raw in gitignore.read_text().splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )


def is_gitignored(path_str: str) -> bool:
    if not _state.gitignore_patterns:
        return False
    p = Path(path_str)
    return any(fnmatch(p.name, pat) or any(fnmatch(part, pat) for part in p.parts) for pat in _state.gitignore_patterns)


def hook(
    events: Event,
    message: str,
    *,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    block: bool = False,
    respect_gitignore: bool = True,
    max_fires: int | None = None,
    tests: InlineTests | None = None,
    async_: bool = False,
    skip_planning_agents: bool | None = None,
) -> None:
    reject_async_decision(events, async_)
    validate_conditions(only_if, "only_if", events)
    validate_conditions(skip_if, "skip_if", events)
    _state.hooks.append(
        RegisteredHook(
            spec=HookSpec(
                events=events,
                only_if=tuple(only_if),
                skip_if=tuple(skip_if),
                message=message,
                block=block,
                respect_gitignore=respect_gitignore,
                max_fires=max_fires,
                tests=tests,
                async_=async_,
                skip_planning_agents=(not block) if skip_planning_agents is None else skip_planning_agents,
            ),
            name=hook_name("hook", None, message),
            source_file=caller_file(),
        )
    )


def on(
    events: Event,
    *,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    respect_gitignore: bool = True,
    max_fires: int | None = None,
    tests: InlineTests | None = None,
    async_: bool = False,
    skip_planning_agents: bool = True,
) -> Callable[[HookHandler], HookHandler]:
    reject_async_decision(events, async_)
    validate_conditions(only_if, "only_if", events)
    validate_conditions(skip_if, "skip_if", events)
    spec = HookSpec(
        events=events,
        only_if=tuple(only_if),
        skip_if=tuple(skip_if),
        respect_gitignore=respect_gitignore,
        max_fires=max_fires,
        tests=tests,
        async_=async_,
        skip_planning_agents=skip_planning_agents,
    )

    def decorator(fn: HookHandler) -> HookHandler:
        validate_handler_signature(fn)
        _state.hooks.append(
            RegisteredHook(
                spec=spec,
                handler=fn,
                name=fn.__name__,
                source_file=fn.__code__.co_filename,
            )
        )
        return fn

    return decorator


def is_planning_agent_skip(spec: HookSpec, evt: BaseHookEvent) -> bool:
    from captain_hook.settings import DEFAULT_PLANNING_AGENTS

    if not spec.skip_planning_agents:
        return False
    if evt.event not in (Event.SubagentStop | Event.SubagentStart):
        return False
    names = settings.planning_agents if (settings := _state.settings) else DEFAULT_PLANNING_AGENTS
    return bool(evt.agent_type and evt.agent_type in names)


def get_matching_hooks(evt: BaseHookEvent) -> list[RegisteredHook]:
    return [
        h
        for h in _state.hooks
        if evt.event in h.spec.events
        and not is_planning_agent_skip(h.spec, evt)
        and matches_conditions(h.spec, evt)
        and (
            not h.spec.respect_gitignore
            or not _state.gitignore_patterns
            or not evt.file
            or not is_gitignored(str(evt.file))
        )
    ]
