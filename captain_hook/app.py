from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, get_args

from captain_hook.conditions import matches_conditions
from captain_hook.state import caller_file, hook_name
from captain_hook.types import (
    Agent,
    Command,
    Content,
    CustomCondition,
    Event,
    FilePath,
    HookSpec,
    InlineTests,
    Pattern,
    RegisteredHook,
    Runs,
    SourceEdits,
    TCondition,
    TestFile,
    Tool,
    ToolInput,
    WorkflowScript,
)

if TYPE_CHECKING:
    from cc_transcript.activity import UserClassifier

    from captain_hook.events import BaseHookEvent
    from captain_hook.settings import HooksSettings
    from captain_hook.types import HookResult

HookHandler = Callable[["BaseHookEvent"], "HookResult | None"]

VALID_CONDITION_TYPES = tuple(t for t in get_args(TCondition) if t is not CustomCondition)
VALID_CONDITION_NAMES = ", ".join(t.__name__ for t in VALID_CONDITION_TYPES) + ", or a CustomCondition"

_TOOL_EVENTS = Event.PreToolUse | Event.PostToolUse | Event.PostToolUseFailure | Event.PermissionRequest

# Conditions that read the current event's tool input can only match on a tool event; a
# condition absent from this map (transcript-history conditions, InPlanMode, Waiting,
# combinators, CustomCondition) reads session state and is valid on every event.
_CONDITION_EVENTS: dict[type, Event] = {
    Tool: _TOOL_EVENTS,
    ToolInput: _TOOL_EVENTS,
    WorkflowScript: _TOOL_EVENTS,
    Command: _TOOL_EVENTS,
    Runs: _TOOL_EVENTS,
    FilePath: _TOOL_EVENTS,
    Content: _TOOL_EVENTS,
    Pattern: _TOOL_EVENTS,
    TestFile: _TOOL_EVENTS,
    SourceEdits: _TOOL_EVENTS,
    Agent: _TOOL_EVENTS | Event.SubagentStart | Event.SubagentStop,
}


def validate_conditions(conditions: Sequence[TCondition], label: str, events: Event | None = None) -> None:
    for c in conditions:
        if not isinstance(c, (*VALID_CONDITION_TYPES, CustomCondition)):
            raise TypeError(
                f"Invalid condition in {label}: {c!r} (type {type(c).__name__}). "
                f"Expected one of: {VALID_CONDITION_NAMES}."
            )
        if events is not None and (valid := _CONDITION_EVENTS.get(type(c))) is not None and not (events & valid):
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


_state = State()


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
    skip_planning_agents: bool = True,
) -> None:
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
                skip_planning_agents=skip_planning_agents,
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
