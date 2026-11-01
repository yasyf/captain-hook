from __future__ import annotations

import json
import sys
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from cc_transcript.query import Session

from captain_hook.app import State, current_state, get_hook_candidates, use_state
from captain_hook.conditions import matches_conditions
from captain_hook.context import HookContext
from captain_hook.events import PreToolUseEvent
from captain_hook.loader import import_pack_module, stamp_identity
from captain_hook.results import Envelope, combine, envelope_text, run_declarative
from captain_hook.session import SessionStore
from captain_hook.types import Event, HookResult


def check(raw: dict[str, Any]) -> Envelope | None:
    root = Path(raw["cwd"])
    rules = Path(__file__).parent / "builtin_packs" / "performance" / "hooks" / "search.py"
    state = State(hooks=list(current_state().hooks))
    start = len(state.hooks)
    with use_state(state):
        import_pack_module("captain_hook._search_guard_rules", rules)
        stamp_identity(start, root=rules.parent, pack="performance")
        owned = {id(entry) for entry in state.hooks[start:]}
        context = HookContext(
            session=SessionStore(None),
            transcript=Session(()),
            settings=None,
            project_root=root,
        )
        event = PreToolUseEvent(_raw=raw, ctx=context)
        entries = [
            entry
            for entry in get_hook_candidates(event)
            if id(entry) in owned and matches_conditions(entry.spec, event)
        ]
        futures: list[Future[HookResult | None]] = []
        for entry in entries:
            result: Future[HookResult | None] = Future()
            result.set_result(run_declarative(entry.spec, event))
            futures.append(result)
        return combine(Event.PreToolUse, entries, futures, 0)


def main() -> None:
    if output := check(json.load(sys.stdin)):
        print(envelope_text(output))
