from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import _state
from captain_hook.conditions import matches_conditions
from captain_hook.events import StopEvent
from captain_hook.testing.snapshots import FixtureOwner
from tests.helpers import build_ctx, make_event, raw_text, raw_tool_msg

QUESTIONS = "captain_hook.builtin_packs.general.hooks.questions"
CLOSING = raw_text("assistant", "Still yours to decide: the pool PRs.")
BOARD = raw_tool_msg("Bash", {"command": "cc-present start --session s --doc d"})


@pytest.fixture
def snapshot_owner() -> Iterator[FixtureOwner]:
    owner = FixtureOwner()
    try:
        yield owner
    finally:
        owner.close()


def write_lines(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def narrate_gate_runs(
    tmp_path: Path,
    owner: FixtureOwner,
    root: list[dict[str, Any]],
    sidechain: list[dict[str, Any]],
    raw: dict[str, Any] | None = None,
) -> bool:
    if (module := sys.modules.get(QUESTIONS)) is not None:
        importlib.reload(module)
    else:
        importlib.import_module(QUESTIONS)
    if sidechain:
        (subagents := tmp_path / "session" / "subagents").mkdir(parents=True)
        write_lines(subagents / "agent-a1.jsonl", sidechain)
    session = write_lines(tmp_path / "session.jsonl", root)
    gate = next(registered for registered in _state.hooks if registered.name.endswith("narrate_then_wait"))
    evt = make_event(StopEvent, raw, ctx=build_ctx(transcript=owner.load(session)))
    evt.__dict__["disallowed_tools"] = frozenset()
    return matches_conditions(gate.spec, evt)


def test_a_board_only_a_subagent_started_leaves_the_gate_on(tmp_path: Path, snapshot_owner: FixtureOwner) -> None:
    assert narrate_gate_runs(tmp_path, snapshot_owner, [CLOSING], [BOARD]) is True


def test_a_board_the_session_started_stands_the_gate_down(tmp_path: Path, snapshot_owner: FixtureOwner) -> None:
    assert narrate_gate_runs(tmp_path, snapshot_owner, [BOARD, CLOSING], []) is False


def test_the_stop_that_continues_a_block_passes(tmp_path: Path, snapshot_owner: FixtureOwner) -> None:
    assert narrate_gate_runs(tmp_path, snapshot_owner, [CLOSING], [], {"stop_hook_active": True}) is False
