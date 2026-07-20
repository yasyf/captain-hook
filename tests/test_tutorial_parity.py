from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
from widget_compiler import compile_fragment  # noqa: E402

from captain_hook.app import _state  # noqa: E402
from captain_hook.dispatch import dispatch  # noqa: E402
from captain_hook.testing.helpers import input_to_event, isolated_state_root  # noqa: E402
from captain_hook.testing.types import Input  # noqa: E402
from captain_hook.types import Event  # noqa: E402

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None and not os.environ.get("CI"), reason="node absent and CI unset")


def normalize(envelope: dict[str, Any] | None) -> dict[str, Any]:
    """Decode a Claude Code stdout envelope into the {action, message, rewritten} verdict shape."""
    if envelope is None:
        return {"action": "pass", "message": None, "rewritten": None}
    if envelope.get("decision") == "block":
        return {"action": "block", "message": envelope.get("reason"), "rewritten": None}
    hso = envelope.get("hookSpecificOutput", {})
    match hso.get("permissionDecision"):
        case "deny":
            return {"action": "block", "message": hso.get("permissionDecisionReason"), "rewritten": None}
        case "allow" if "updatedInput" in hso:
            return {
                "action": "rewrite",
                "message": hso.get("additionalContext"),
                "rewritten": hso["updatedInput"].get("command"),
            }
        case "allow":
            return {"action": "allow", "message": hso.get("additionalContext"), "rewritten": None}
    if "additionalContext" in hso:
        return {"action": "warn", "message": hso["additionalContext"], "rewritten": None}
    return {"action": "pass", "message": None, "rewritten": None}


def python_verdict(fragment: str, event: Event, case_input: dict[str, Any]) -> dict[str, Any]:
    """Compile a fragment's hooks and run one input through the real dispatch engine, isolated."""
    saved = list(_state.hooks)
    try:
        compile_fragment(FRAGMENTS / f"{fragment}.py")
        with isolated_state_root():
            return normalize(dispatch(event, input_to_event(event, Input(**case_input))))
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


@cache
def live_results(widget_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    widget = MATRIX["widgets"][widget_id]
    event = Event[widget["event"]]
    saved = list(_state.hooks)
    try:
        compiled = compile_fragment(FRAGMENTS / f"{widget['fragment']}.py")
        python: dict[str, dict[str, Any]] = {}
        with isolated_state_root():
            for case in widget["cases"]:
                if case["check"] == "parity":
                    python[case["id"]] = normalize(dispatch(event, input_to_event(event, Input(**case["input"]))))
    finally:
        _state.hooks[:] = saved
    node_cases = [{"id": c["id"], "input": {"event": widget["event"], **c["input"]}} for c in widget["cases"]]
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
    verdict = python_verdict(widget["fragment"], Event[case.get("event", "PreToolUse")], case["input"])
    assert verdict == case["verdict"]


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
    "used_skill": "from captain_hook import nudge, UsedSkill\nnudge('x', skip_if=[UsedSkill('codex')])\n",
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
