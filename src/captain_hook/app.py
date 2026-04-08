from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, get_args

from pydantic_settings import BaseSettings

from captain_hook.conditions import matches_conditions
from captain_hook.types import (
    CustomCondition,
    Event,
    HookSpec,
    RegisteredHook,
    TCondition,
    TTest,
)

if TYPE_CHECKING:
    from captain_hook.classifiers import MessageClassifier
    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult

HookHandler = Callable[["BaseHookEvent"], "HookResult | None"]

VALID_CONDITION_TYPES = tuple(
    t for t in get_args(TCondition) if t is not CustomCondition
)
VALID_CONDITION_NAMES = ", ".join(t.__name__ for t in VALID_CONDITION_TYPES) + ", or a CustomCondition"

CONF_MODULE = "conf"


def validate_conditions(conditions: Sequence[TCondition], label: str) -> None:
    for c in conditions:
        if not isinstance(c, (*VALID_CONDITION_TYPES, CustomCondition)):
            raise TypeError(
                f"Invalid condition in {label}: {c!r} (type {type(c).__name__}). "
                f"Expected one of: {VALID_CONDITION_NAMES}."
            )


def validate_handler_signature(fn: HookHandler) -> None:
    sig = inspect.signature(fn)
    params = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(params) != 1:
        raise TypeError(
            f"Handler {fn.__name__} has wrong signature: expected (evt) -> HookResult | None, "
            f"got {sig}. Hook handlers must accept exactly one positional parameter (the event)."
        )
    required_kw = [
        p for p in sig.parameters.values()
        if p.kind == inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    ]
    if required_kw:
        names = ", ".join(p.name for p in required_kw)
        raise TypeError(
            f"Handler {fn.__name__} has required keyword-only parameter(s): {names}. "
            f"Hook handlers are called as handler(evt) — keyword-only parameters must have defaults."
        )


@dataclass
class State:
    hooks: list[RegisteredHook] = field(default_factory=list)
    gitignore_patterns: list[str] = field(default_factory=list)
    settings: BaseSettings | None = None
    classifier: MessageClassifier | None = None
    counter: int = field(default=0, repr=False)


_state = State()


def reset() -> None:
    _state.hooks.clear()
    _state.gitignore_patterns.clear()
    _state.counter = 0
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
    return any(
        fnmatch(p.name, pat) or any(fnmatch(part, pat) for part in p.parts) for pat in _state.gitignore_patterns
    )


def hook(
    events: Event,
    *,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),

    message: str | None = None,
    block: bool = False,
    respect_gitignore: bool = True,
    max_fires: int | None = None,
    tests: TTest | None = None,
    async_: bool = False,
) -> None:
    """Register a declarative hook with a fixed message."""
    if message is None:
        raise TypeError(
            "hook() requires message= for declarative hooks. "
            "Provide message='...' or use @on() for handler-based hooks."
        )
    validate_conditions(only_if, "only_if")
    validate_conditions(skip_if, "skip_if")
    _state.counter += 1
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
            ),
            name=f"declarative_{_state.counter}",
        )
    )


def on(
    events: Event,
    *,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    respect_gitignore: bool = True,
    max_fires: int | None = None,
    tests: TTest | None = None,
    async_: bool = False,
) -> Callable[[HookHandler], HookHandler]:
    """Decorator to register a handler-based hook."""
    validate_conditions(only_if, "only_if")
    validate_conditions(skip_if, "skip_if")
    spec = HookSpec(
        events=events,
        only_if=tuple(only_if),
        skip_if=tuple(skip_if),
        respect_gitignore=respect_gitignore,
        max_fires=max_fires,
        tests=tests,
        async_=async_,
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


def register(
    events: Event,
    *,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    message: str | None = None,
    block: bool = False,
    respect_gitignore: bool = True,
    max_fires: int | None = None,
    tests: TTest | None = None,
    async_: bool = False,
) -> Callable[[HookHandler], HookHandler] | None:
    """Register a hook — declarative with ``message`` or as a decorator without."""
    validate_conditions(only_if, "only_if")
    validate_conditions(skip_if, "skip_if")

    if message is not None:
        hook(
            events,
            only_if=only_if,
            skip_if=skip_if,
            message=message,
            block=block,
            respect_gitignore=respect_gitignore,
            max_fires=max_fires,
            tests=tests,
            async_=async_,
        )
        return None

    if block:
        raise TypeError(
            "hook() called with block=True but no message= provided. "
            "Declarative hooks require message= to specify the block reason. "
            "Either provide message='...' or use @on() for handler-based hooks."
        )

    spec = HookSpec(
        events=events,
        only_if=tuple(only_if),
        skip_if=tuple(skip_if),
        block=block,
        respect_gitignore=respect_gitignore,
        max_fires=max_fires,
        tests=tests,
        async_=async_,
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


def get_matching_hooks(evt: BaseHookEvent) -> list[RegisteredHook]:
    return [
        h
        for h in _state.hooks
        if evt.event in h.spec.events
        and matches_conditions(h.spec, evt)
        and (
            not h.spec.respect_gitignore
            or not _state.gitignore_patterns
            or not evt.file
            or not is_gitignored(str(evt.file))
        )
    ]


def build_hook_settings(module: ModuleType) -> BaseSettings:
    if importlib.util.find_spec("captain_hook.settings"):
        settings_mod = importlib.import_module("captain_hook.settings")
        return settings_mod.build_settings(module)
    return module  # type: ignore[return-value]


def import_or_reload(fqn: str, fresh_this_pass: set[str]) -> ModuleType:
    if fqn in fresh_this_pass:
        return sys.modules[fqn]
    before = set(sys.modules)
    if fqn in sys.modules:
        mod = importlib.reload(sys.modules[fqn])
    else:
        mod = importlib.import_module(fqn)
    fresh_this_pass.update(set(sys.modules) - before)
    fresh_this_pass.add(fqn)
    return mod


def discover_hooks(hooks_dir: str | Path) -> None:
    hooks_path = Path(hooks_dir).resolve()
    if str(hooks_path.parent) not in sys.path:
        sys.path.insert(0, str(hooks_path.parent))

    pkg = hooks_path.name
    fresh_this_pass: set[str] = set()

    top_level = {info.name for info in pkgutil.iter_modules([str(hooks_path)]) if not info.name.startswith("_")}

    if CONF_MODULE in top_level:
        conf_module = import_or_reload(f"{pkg}.{CONF_MODULE}", fresh_this_pass)
        _state.settings = build_hook_settings(conf_module)
        if classifier := getattr(conf_module, "classifier", None):
            _state.classifier = classifier

    all_modules = {
        info.name
        for info in pkgutil.walk_packages([str(hooks_path)], prefix=f"{pkg}.")
        if not info.name.rpartition(".")[2].startswith("_")
    }

    for fqn in sorted(all_modules - {f"{pkg}.{CONF_MODULE}"}):
        import_or_reload(fqn, fresh_this_pass)
