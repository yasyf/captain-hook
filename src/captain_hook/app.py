from __future__ import annotations

import contextvars
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
from typing import TYPE_CHECKING, Any, get_args

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
    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult

HookHandler = Callable[["BaseHookEvent"], "HookResult | None"]

VALID_CONDITION_TYPES = tuple(
    t for t in get_args(TCondition) if t is not CustomCondition
)
VALID_CONDITION_NAMES = ", ".join(t.__name__ for t in VALID_CONDITION_TYPES) + ", or a CustomCondition"


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

CONF_MODULE = "conf"

_current_app: contextvars.ContextVar[HookApp | None] = contextvars.ContextVar(
    "_current_app",
    default=None,
)


def get_current_app() -> HookApp:
    """Return the HookApp bound to the current context.

    Raises:
        RuntimeError: If no app is active (outside ``discover_hooks`` or manual context).

    Returns:
        The active HookApp instance.
    """
    if app := _current_app.get():
        return app
    raise RuntimeError(
        "No active HookApp context. Primitives like nudge(), gate(), lint(), "
        "block_command() must be called during HookApp.discover_hooks() or "
        "inside a manually set HookApp context."
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
) -> Callable[[HookHandler], HookHandler] | None:
    """Register a hook on the current app (module-level convenience).

    Behaves identically to ``app.register()``: if ``message`` is provided,
    registers a declarative hook and returns ``None``; otherwise returns a
    decorator for a handler function.

    Args:
        events: Event flags to match (combinable with ``|``).
        only_if: Conditions that must all match for the hook to fire.
        skip_if: Conditions that suppress the hook if any match.
        message: Static message for declarative mode. Omit for handler mode.
        block: If True, the hook blocks instead of warning (declarative mode).
        respect_gitignore: Skip gitignored files when True.
        max_fires: Limit how many times this hook fires per session.
        tests: Inline test dict mapping ``Input`` to ``Block``/``Warn``/``Allow``.
        async_: If True, hook runs in the async dispatch pass.

    Returns:
        None for declarative hooks, or a decorator for handler hooks.

    Example:
        Declarative (static message):
            >>> hook(Event.PreToolUse, only_if=[Tool("Bash")], message="blocked", block=True)

        Handler (dynamic logic):
            >>> @hook(Event.PreToolUse, only_if=[Tool("Edit")])
            ... def check_edit(evt):
            ...     return evt.block("not allowed") if evt.file_matches("*.lock") else None
    """
    return get_current_app().register(
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


@dataclass
class HookApp:
    """Central registry for hook definitions and discovery.

    Provides two registration modes:

    **Declarative** — static message, no handler function::

        app.hook(Event.PreToolUse, only_if=[Tool("Bash"), Command(r"rm")],
                 message="rm is blocked", block=True)

    **Handler** — decorated function returning ``HookResult | None``::

        @app.on(Event.PostToolUse, only_if=[Tool("Edit"), FilePath("*.py")])
        def check_style(evt):
            return evt.warn("check style") if "TODO" in (evt.content or "") else None

    Use ``discover_hooks`` to load hook modules from a directory, or register
    hooks manually via ``hook()`` / ``on()``.
    """

    hooks: list[RegisteredHook] = field(default_factory=lambda: [])
    gitignore_patterns: list[str] = field(default_factory=lambda: [])
    settings: Any = None
    classifier: Any = None
    counter: int = field(default=0, repr=False)

    def reset(self) -> None:
        self.hooks.clear()
        self.gitignore_patterns.clear()
        self.counter = 0
        self.settings = None
        self.classifier = None

    def load_gitignore(self, root: Path) -> None:
        self.gitignore_patterns.clear()
        if not (gitignore := root / ".gitignore").exists():
            return
        self.gitignore_patterns.extend(
            line.rstrip("/")
            for raw in gitignore.read_text().splitlines()
            if (line := raw.strip()) and not line.startswith("#")
        )

    def is_gitignored(self, path_str: str) -> bool:
        if not self.gitignore_patterns:
            return False
        p = Path(path_str)
        return any(
            fnmatch(p.name, pat) or any(fnmatch(part, pat) for part in p.parts) for pat in self.gitignore_patterns
        )

    def hook(
        self,
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
        if message is None:
            raise TypeError(
                "hook() requires message= for declarative hooks. "
                "Provide message='...' or use @app.on() for handler-based hooks."
            )
        validate_conditions(only_if, "only_if")
        validate_conditions(skip_if, "skip_if")
        self.counter += 1
        self.hooks.append(
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
                name=f"declarative_{self.counter}",
            )
        )

    def on(
        self,
        events: Event,
        *,
        only_if: Sequence[TCondition] = (),
        skip_if: Sequence[TCondition] = (),
        respect_gitignore: bool = True,
        max_fires: int | None = None,
        tests: TTest | None = None,
        async_: bool = False,
    ) -> Callable[[HookHandler], HookHandler]:
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
            self.hooks.append(
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
        self,
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
        validate_conditions(only_if, "only_if")
        validate_conditions(skip_if, "skip_if")

        if message is not None:
            self.hook(
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
                "Either provide message='...' or use @app.on() for handler-based hooks."
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
            self.hooks.append(
                RegisteredHook(
                    spec=spec,
                    handler=fn,
                    name=fn.__name__,
                    source_file=fn.__code__.co_filename,
                )
            )
            return fn

        return decorator

    def get_matching_hooks(self, evt: BaseHookEvent) -> list[RegisteredHook]:
        return [
            h
            for h in self.hooks
            if evt.event in h.spec.events
            and matches_conditions(h.spec, evt)
            and (
                not h.spec.respect_gitignore
                or not self.gitignore_patterns
                or not evt.file
                or not self.is_gitignored(str(evt.file))
            )
        ]

    @staticmethod
    def _build_settings(module: ModuleType) -> Any:
        if importlib.util.find_spec("captain_hook.settings"):
            settings_mod = importlib.import_module("captain_hook.settings")
            return settings_mod.build_settings(module)  # type: ignore[no-any-return]
        return module

    @staticmethod
    def _import_or_reload(fqn: str, fresh_this_pass: set[str]) -> ModuleType:
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

    def discover_hooks(self, hooks_dir: str | Path) -> None:
        hooks_path = Path(hooks_dir).resolve()
        if str(hooks_path.parent) not in sys.path:
            sys.path.insert(0, str(hooks_path.parent))

        pkg = hooks_path.name
        fresh_this_pass: set[str] = set()
        token = _current_app.set(self)
        try:
            top_level = {info.name for info in pkgutil.iter_modules([str(hooks_path)]) if not info.name.startswith("_")}

            if CONF_MODULE in top_level:
                conf_module = self._import_or_reload(f"{pkg}.{CONF_MODULE}", fresh_this_pass)
                self.settings = self._build_settings(conf_module)
                if classifier := getattr(conf_module, "classifier", None):
                    self.classifier = classifier

            all_modules = {
                info.name
                for info in pkgutil.walk_packages([str(hooks_path)], prefix=f"{pkg}.")
                if not info.name.rpartition(".")[2].startswith("_")
            }

            for fqn in sorted(all_modules - {f"{pkg}.{CONF_MODULE}"}):
                self._import_or_reload(fqn, fresh_this_pass)
        finally:
            _current_app.reset(token)
