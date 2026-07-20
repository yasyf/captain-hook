from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from cc_transcript.tools import expand_tool_names

from captain_hook.app import _state
from captain_hook.types import (
    And,
    Command,
    Content,
    FilePath,
    Not,
    Or,
    RanCommand,
    Runs,
    Tool,
    TouchedFile,
    UsedSkill,
    Waiting,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from captain_hook.types import RegisteredHook, TCondition

# gate/nudge lower to a closure carrying exactly these freevars; a non-None sig or when is
# session/transcript dynamics the emulator can't model, so those refuse.
GATE_FREEVARS = frozenset({"block", "message", "sig", "when"})
REWRITE_FREEVARS = frozenset({"pattern", "replace", "note"})

# Python-only regex syntax with no JS equivalent — refused so parity can't silently diverge.
INLINE_FLAG = re.compile(r"\(\?[aiLmsux]+[):]")
FORBIDDEN_REGEX = ("(?P<", "(?P=", "\\A", "\\Z")


def check_regex_dialect(pattern: str) -> str:
    if INLINE_FLAG.search(pattern) or any(tok in pattern for tok in FORBIDDEN_REGEX):
        raise ValueError(f"regex {pattern!r} uses Python-only syntax outside the JS-shared subset")
    return pattern


def closure_freevars(fn: Callable[..., Any]) -> dict[str, Any]:
    return {n: cell.cell_contents for n, cell in zip(fn.__code__.co_freevars, fn.__closure__ or (), strict=True)}


def load_hooks(path: Path) -> list[RegisteredHook]:
    """Execute a fragment under the real registration machinery, returning its registered hooks."""
    _state.hooks.clear()
    exec(compile(path.read_text(), str(path), "exec"), {"__name__": "__fragment__", "__file__": str(path)})
    return list(_state.hooks)


def serialize_condition(c: TCondition) -> dict[str, Any]:
    match c:
        case Tool(names):
            return {"kind": "Tool", "names": sorted({alias for n in names for alias in expand_tool_names(n)})}
        case Command(pattern):
            return {"kind": "Command", "pattern": check_regex_dialect(pattern)}
        case Runs(argv):
            return {"kind": "Runs", "argv": list(argv)}
        case FilePath(patterns, project_only):
            return {"kind": "FilePath", "patterns": list(patterns), "project_only": project_only}
        case Content(pattern, project_only):
            return {"kind": "Content", "pattern": check_regex_dialect(pattern), "project_only": project_only}
        case TouchedFile(patterns, _):
            return {"kind": "TouchedFile", "patterns": list(patterns)}
        case UsedSkill(names, _):
            return {"kind": "UsedSkill", "names": list(names)}
        case RanCommand(argv, _):
            return {"kind": "RanCommand", "argv": list(argv)}
        case Waiting():
            return {"kind": "Waiting"}
        case Not(condition):
            return {"kind": "Not", "condition": serialize_condition(condition)}
        case Or(conditions):
            return {"kind": "Or", "conditions": [serialize_condition(sub) for sub in conditions]}
        case And(conditions):
            return {"kind": "And", "conditions": [serialize_condition(sub) for sub in conditions]}
        case _:
            raise ValueError(f"the emulator cannot serialize condition {type(c).__name__}")


def lowered_payload(entry: RegisteredHook) -> dict[str, Any]:
    """The message/block/rewrite fields, from the spec (declarative) or the handler closure."""
    if entry.handler is None:
        return {"message": entry.spec.message, "block": entry.spec.block}
    fv = closure_freevars(entry.handler)
    if GATE_FREEVARS <= fv.keys():
        if fv["sig"] is not None or fv["when"] is not None:
            raise ValueError(
                f"the emulator cannot serialize a gate/nudge with signals or a when= predicate ({entry.name!r})"
            )
        return {"message": fv["message"], "block": fv["block"]}
    if entry.handler.__qualname__.endswith(".regex_handler") and REWRITE_FREEVARS <= fv.keys():
        return {
            "message": None,
            "block": False,
            "rewrite": {"pattern": check_regex_dialect(fv["pattern"]), "replace": fv["replace"], "note": fv["note"]},
        }
    raise ValueError(f"the emulator cannot serialize handler-backed hook {entry.name!r} ({entry.handler.__qualname__})")


def serialize_hook(entry: RegisteredHook) -> dict[str, Any]:
    return {
        "events": [e.name for e in entry.spec.events],
        **lowered_payload(entry),
        "only_if": [serialize_condition(c) for c in entry.spec.only_if],
        "skip_if": [serialize_condition(c) for c in entry.spec.skip_if],
    }


def compile_fragment(path: Path) -> dict[str, Any]:
    """Compile a docs fragment to the ``{"hooks": [...]}`` JSON the emulator bundle evaluates.

    Leaves the live ``RegisteredHook`` objects in ``captain_hook.app._state.hooks`` so a caller
    can dispatch them through the real Python engine for parity comparison.
    """
    return {"hooks": [serialize_hook(entry) for entry in load_hooks(path)]}
