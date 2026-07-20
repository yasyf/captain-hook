from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "docs" / "scripts"
FRAGMENTS = ROOT / "docs" / "_fragments"
SRC = ROOT / "docs" / "tutorial" / "_src"
PARITY_MJS = SRC / "parity.mjs"
MATRIX = json.loads((SRC / "matrix.json").read_text())

sys.path.insert(0, str(SCRIPTS))

from build_emulator import BANNER_PREFIX, BUNDLE, src_hash  # noqa: E402
from widget_compiler import compile_fragment, load_hooks  # noqa: E402

import captain_hook  # noqa: E402
from captain_hook.app import _state  # noqa: E402
from captain_hook.dispatch import dispatch  # noqa: E402
from captain_hook.loader import discover_pack  # noqa: E402
from captain_hook.testing.helpers import input_to_event, isolated_state_root  # noqa: E402
from captain_hook.testing.types import Input  # noqa: E402
from captain_hook.types import Event  # noqa: E402

PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None and not os.environ.get("CI"), reason="node absent and CI unset")


def normalize(envelope: dict[str, Any] | None) -> dict[str, Any]:
    """Decode a Claude Code stdout envelope into the {action, message, rewritten} verdict shape.

    The lowered declarative model only ever emits block / warn / rewrite / pass, so a bare
    ``permissionDecision: allow`` (indistinguishable from a warn's approve rider) maps to warn.
    """
    if envelope is None:
        return {"action": "pass", "message": None, "rewritten": None}
    if envelope.get("decision") == "block":
        return {"action": "block", "message": envelope.get("reason"), "rewritten": None}
    hso = envelope.get("hookSpecificOutput", {})
    if hso.get("permissionDecision") == "deny":
        return {"action": "block", "message": hso.get("permissionDecisionReason"), "rewritten": None}
    if "updatedInput" in hso:
        return {
            "action": "rewrite",
            "message": hso.get("additionalContext"),
            "rewritten": hso["updatedInput"].get("command"),
        }
    if "additionalContext" in hso:
        return {"action": "warn", "message": hso["additionalContext"], "rewritten": None}
    return {"action": "pass", "message": None, "rewritten": None}


def canned_verdict(widget: dict[str, Any], event: Event, case: dict[str, Any]) -> dict[str, Any]:
    """Register a canned widget's hooks (a fragment or a shipped pack) and dispatch one input, isolated."""
    saved = list(_state.hooks)
    try:
        _state.hooks.clear()
        if "pack" in widget:
            discover_pack(widget["pack"], PACKS_DIR / widget["pack"] / "hooks")
        else:
            load_hooks(FRAGMENTS / f"{widget['fragment']}.py")
        with isolated_state_root():
            return normalize(dispatch(event, input_to_event(event, Input(**case["input"]))))
    finally:
        _state.hooks[:] = saved


def run_node(hooks: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    proc = subprocess.run(
        ["node", str(PARITY_MJS)],
        input=json.dumps({"hooks": hooks, "cases": cases}),
        capture_output=True,
        text=True,
        check=True,
    )
    return {v["id"]: v["verdict"] for v in json.loads(proc.stdout)["verdicts"]}


def build_transcript(edits: list[str], skills: list[str]) -> list[dict[str, Any]]:
    """Raw transcript lines that make TouchedFile / UsedSkill fire on the given edits and skills."""

    def line(uid: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        block = {"type": "tool_use", "name": name, "input": tool_input, "id": uid}
        return {
            "type": "assistant",
            "uuid": uid,
            "sessionId": "s",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"model": "<t>", "content": [block]},
        }

    return [
        *(line(f"e{i}", "Edit", {"file_path": p, "old_string": "a", "new_string": "b"}) for i, p in enumerate(edits)),
        *(line(f"s{i}", "Skill", {"skill": n}) for i, n in enumerate(skills)),
    ]


def _python_input(case: dict[str, Any]) -> dict[str, Any]:
    inp = dict(case.get("input", {}))
    if "edits" in case or "skills" in case:
        inp["transcript"] = build_transcript(case.get("edits", []), case.get("skills", []))
    return inp


def _js_input(widget: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    inp = {"event": widget["event"], **case.get("input", {})}
    session = dict(case.get("session", {}))
    if "repoRoot" in widget:
        session["repoRoot"] = widget["repoRoot"]
    if session:
        inp["session"] = session
    return inp


@cache
def live_results(widget_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    widget = MATRIX["widgets"][widget_id]
    event = Event[widget["event"]]
    root = widget.get("repoRoot")
    saved = list(_state.hooks)
    try:
        compiled = compile_fragment(FRAGMENTS / f"{widget['fragment']}.py")
        python: dict[str, dict[str, Any]] = {}
        with isolated_state_root():
            for case in widget["cases"]:
                if case["check"] == "parity":
                    evt = input_to_event(event, Input(**_python_input(case)))
                    if root:
                        evt.ctx = replace(evt.ctx, project_root=Path(root))
                    python[case["id"]] = normalize(dispatch(event, evt))
    finally:
        _state.hooks[:] = saved
    node_cases = [{"id": c["id"], "input": _js_input(widget, c)} for c in widget["cases"]]
    return {"python": python, "js": run_node(compiled["hooks"], node_cases)}


def _widget_cases(mode: str) -> list[Any]:
    return [
        pytest.param(widget_id, case, id=f"{widget_id}:{case['id']}")
        for widget_id, widget in MATRIX["widgets"].items()
        if widget["mode"] == mode
        for case in widget["cases"]
    ]


def test_bundle_drift() -> None:
    banner = BUNDLE.read_text().splitlines()[0]
    assert banner == f"{BANNER_PREFIX}{src_hash()}", (
        "committed emulator.js is stale — run `python docs/scripts/build_emulator.py`"
    )


@requires_node
@pytest.mark.parametrize(("widget_id", "case"), _widget_cases("live"))
def test_parity(widget_id: str, case: dict[str, Any]) -> None:
    results = live_results(widget_id)
    verdict = results["js"][case["id"]]
    if case["check"] == "honesty":
        assert verdict["action"] == "subset-exceeded"
        assert "capt-hook test" in (verdict["message"] or "")
    else:
        assert verdict == results["python"][case["id"]]


@pytest.mark.parametrize(("widget_id", "case"), _widget_cases("canned"))
def test_canned_verdicts(widget_id: str, case: dict[str, Any]) -> None:
    if not case.get("verified"):
        pytest.skip("illustrative recording, not engine-verified")
    widget = MATRIX["widgets"][widget_id]
    event = Event[case.get("event", widget.get("event", "PreToolUse"))]
    assert canned_verdict(widget, event, case) == case["verdict"]


REFUSALS = {
    "gate_signals": "from captain_hook import gate\nfrom captain_hook.types import Signal, Signals\n"
    "gate('x', signals=Signals([Signal(pattern='y')], threshold=1))\n",
    "nudge_when": "from captain_hook import nudge\nnudge('x', when=lambda e: True)\n",
    "rewrite_structural": "from captain_hook import rewrite_command\n"
    "rewrite_command('cat $$$A', 'bat $$$A', note='x')\n",
    "rewrite_to": "from captain_hook import rewrite_command\n"
    "rewrite_command(only_if=[], to=lambda e: None, block='no')\n",
    "in_plan_mode": "from captain_hook import Event, hook\nfrom captain_hook.types import InPlanMode\n"
    "hook(Event.PreToolUse, 'x', only_if=[InPlanMode()], block=True)\n",
    "from_subagent": "from captain_hook import Event, hook\nfrom captain_hook.types import FromSubagent\n"
    "hook(Event.PreToolUse, 'x', only_if=[FromSubagent()], block=True)\n",
    "ast_grep_pattern": "from captain_hook import Event, hook\nfrom captain_hook.types import Pattern\n"
    "hook(Event.PreToolUse, 'x', only_if=[Pattern('eval($$$)')], block=True)\n",
    "regex_dialect": "from captain_hook import Event, hook\nfrom captain_hook.types import Command\n"
    "hook(Event.PreToolUse, 'x', only_if=[Command('(?P<n>git)')], block=True)\n",
}


@pytest.mark.parametrize("source", REFUSALS.values(), ids=list(REFUSALS))
def test_compiler_refuses(source: str, tmp_path: Path) -> None:
    frag = tmp_path / "fragment.py"
    frag.write_text(source)
    saved = list(_state.hooks)
    try:
        with pytest.raises(ValueError):
            compile_fragment(frag)
    finally:
        _state.hooks[:] = saved


def _compile_source(source: str, tmp_path: Path) -> list[dict[str, Any]]:
    frag = tmp_path / "fragment.py"
    frag.write_text(source)
    saved = list(_state.hooks)
    try:
        return compile_fragment(frag)["hooks"]
    finally:
        _state.hooks[:] = saved


def test_compiler_lowers_gate(tmp_path: Path) -> None:
    hooks = _compile_source(
        "from captain_hook import Event, Runs, gate\n"
        "gate('run tests', only_if=[Runs('git', 'push')], events=Event.PreToolUse)\n",
        tmp_path,
    )
    assert hooks == [
        {
            "events": ["PreToolUse"],
            "message": "run tests",
            "block": True,
            "only_if": [{"kind": "Runs", "argv": ["git", "push"]}],
            "skip_if": [],
        }
    ]


def test_compiler_lowers_rewrite(tmp_path: Path) -> None:
    (hook,) = _compile_source(
        "from captain_hook import rewrite_command\n"
        'rewrite_command(r"^cat\\s+(\\S+)$", r"ccx read \\1 --full", note="x")\n',
        tmp_path,
    )
    assert hook["rewrite"] == {"pattern": r"^cat\s+(\S+)$", "replace": r"ccx read \1 --full", "note": "x"}
    assert {c["kind"] for c in hook["only_if"]} == {"Tool", "Command"}


INLINE_TEST_FRAGMENTS = ["tutorial_first_block_tested", "tutorial_runs_guard"]


@pytest.mark.parametrize("fragment", INLINE_TEST_FRAGMENTS)
def test_fragment_inline_tests(fragment: str) -> None:
    """Run a fragment's own ``tests={...}`` through the real engine, loaded via the compiler seam."""
    from captain_hook.testing.helpers import run_inline_tests

    saved = list(_state.hooks)
    try:
        load_hooks(FRAGMENTS / f"{fragment}.py")
        results = run_inline_tests()
    finally:
        _state.hooks[:] = saved
    assert results, f"{fragment} registered no inline tests"
    assert [(name, msg) for name, _status, ok, msg in results if not ok] == []
