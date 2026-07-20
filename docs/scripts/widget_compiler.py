from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from cc_transcript.tools import expand_tool_names

from captain_hook.app import _state
from captain_hook.types import And, Command, Content, FilePath, Not, Or, Runs, Tool, UsedSkill

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from captain_hook.types import RegisteredHook, TCondition

# gate/nudge lower to handler-backed hooks whose message and block flag live in a closure;
# a call-through recorder captures that lowered pair keyed by the registered hook name.
LOWERED: dict[str, tuple[str, bool]] = {}


@contextmanager
def _record_primitives() -> Iterator[None]:
    import importlib

    import captain_hook

    nudge_mod = importlib.import_module("captain_hook.primitives.nudge")
    real_nudge = nudge_mod.nudge

    def recording_nudge(message: str, *, block: bool = False, **kwargs: Any) -> None:
        before = len(_state.hooks)
        real_nudge(message, block=block, **kwargs)
        for entry in _state.hooks[before:]:
            LOWERED[entry.name] = (message, block)

    patched = {"nudge": recording_nudge, "gate": lambda message, **kw: recording_nudge(message, block=True, **kw)}
    saved = {name: (getattr(captain_hook, name), getattr(nudge_mod, name)) for name in patched}
    for name, fn in patched.items():
        setattr(captain_hook, name, fn)
        setattr(nudge_mod, name, fn)
    try:
        yield
    finally:
        for name, (ch_fn, mod_fn) in saved.items():
            setattr(captain_hook, name, ch_fn)
            setattr(nudge_mod, name, mod_fn)


def load_hooks(path: Path) -> list[RegisteredHook]:
    """Execute a fragment under the real registration machinery, returning its registered hooks."""
    _state.hooks.clear()
    LOWERED.clear()
    with _record_primitives():
        exec(compile(path.read_text(), str(path), "exec"), {"__name__": "__fragment__", "__file__": str(path)})
    return list(_state.hooks)


def serialize_condition(c: TCondition) -> dict[str, Any]:
    match c:
        case Tool(names):
            return {"kind": "Tool", "names": sorted({alias for n in names for alias in expand_tool_names(n)})}
        case Command(pattern):
            return {"kind": "Command", "pattern": pattern}
        case Runs(argv):
            return {"kind": "Runs", "argv": list(argv)}
        case FilePath(patterns, project_only):
            return {"kind": "FilePath", "patterns": list(patterns), "project_only": project_only}
        case Content(pattern, project_only):
            return {"kind": "Content", "pattern": pattern, "project_only": project_only}
        case UsedSkill(names, _):
            return {"kind": "UsedSkill", "names": list(names)}
        case Not(condition):
            return {"kind": "Not", "condition": serialize_condition(condition)}
        case Or(conditions):
            return {"kind": "Or", "conditions": [serialize_condition(sub) for sub in conditions]}
        case And(conditions):
            return {"kind": "And", "conditions": [serialize_condition(sub) for sub in conditions]}
        case _:
            raise ValueError(f"the emulator cannot serialize condition {type(c).__name__}")


def serialize_hook(entry: RegisteredHook) -> dict[str, Any]:
    spec = entry.spec
    if entry.handler is None:
        message, block = spec.message, spec.block
    elif entry.name in LOWERED:
        message, block = LOWERED[entry.name]
    else:
        raise ValueError(f"the emulator cannot serialize handler-backed hook {entry.name!r}")
    return {
        "events": [e.name for e in spec.events],
        "message": message,
        "block": block,
        "only_if": [serialize_condition(c) for c in spec.only_if],
        "skip_if": [serialize_condition(c) for c in spec.skip_if],
    }


def compile_fragment(path: Path) -> dict[str, Any]:
    """Compile a docs fragment to the ``{"hooks": [...]}`` JSON the emulator bundle evaluates.

    Leaves the live ``RegisteredHook`` objects in ``captain_hook.app._state.hooks`` so a caller
    can dispatch them through the real Python engine for parity comparison.
    """
    return {"hooks": [serialize_hook(entry) for entry in load_hooks(path)]}
