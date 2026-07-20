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

from build_emulator import BUNDLE, build  # noqa: E402
from widget_compiler import compile_fragment  # noqa: E402

from captain_hook.app import _state  # noqa: E402
from captain_hook.dispatch import dispatch  # noqa: E402
from captain_hook.testing.helpers import input_to_event, isolated_state_root  # noqa: E402
from captain_hook.testing.types import Input  # noqa: E402
from captain_hook.types import Event  # noqa: E402

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None and not os.environ.get("CI"), reason="node absent and CI unset")


def normalize(envelope: dict[str, Any] | None) -> dict[str, Any]:
    """Decode a Claude Code stdout envelope into the {action, message, command} verdict shape."""
    if envelope is None:
        return {"action": "pass", "message": None, "command": None}
    if envelope.get("decision") == "block":
        return {"action": "block", "message": envelope.get("reason"), "command": None}
    hso = envelope.get("hookSpecificOutput", {})
    match hso.get("permissionDecision"):
        case "deny":
            return {"action": "block", "message": hso.get("permissionDecisionReason"), "command": None}
        case "allow" if "updatedInput" in hso:
            return {
                "action": "rewrite",
                "message": hso.get("additionalContext"),
                "command": hso["updatedInput"].get("command"),
            }
        case "allow":
            return {"action": "allow", "message": hso.get("additionalContext"), "command": None}
    if "additionalContext" in hso:
        return {"action": "warn", "message": hso["additionalContext"], "command": None}
    return {"action": "pass", "message": None, "command": None}


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
def widget_results(widget_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    widget = MATRIX["widgets"][widget_id]
    event = Event[widget.get("event", "PreToolUse")]
    saved = list(_state.hooks)
    try:
        compiled = compile_fragment(FRAGMENTS / f"{widget['fragment']}.py")
        python: dict[str, dict[str, Any]] = {}
        with isolated_state_root():
            for case in widget["cases"]:
                if widget["mode"] == "canned" or case["check"] != "honesty":
                    evt = input_to_event(event, Input(**case["input"]))
                    python[case["id"]] = normalize(dispatch(event, evt))
    finally:
        _state.hooks[:] = saved
    js: dict[str, dict[str, Any]] = {}
    if widget["mode"] != "canned":
        node_cases = [{"id": c["id"], "input": {"event": widget["event"], **c["input"]}} for c in widget["cases"]]
        js = run_node(compiled["hooks"], node_cases)
    return {"python": python, "js": js}


def _cases() -> list[Any]:
    params = []
    for widget_id, widget in MATRIX["widgets"].items():
        for case in widget["cases"]:
            check = "canned" if widget["mode"] == "canned" else case["check"]
            params.append(pytest.param(widget_id, case, check, id=f"{widget_id}:{case['id']}"))
    return params


@requires_node
def test_bundle_drift(tmp_path: Path) -> None:
    rebuilt = build(tmp_path / "emulator.js")
    assert rebuilt.read_bytes() == BUNDLE.read_bytes(), (
        "committed emulator.js is stale — run `python docs/scripts/build_emulator.py`"
    )


@requires_node
@pytest.mark.parametrize(("widget_id", "case", "check"), _cases())
def test_parity(widget_id: str, case: dict[str, Any], check: str) -> None:
    results = widget_results(widget_id)
    cid = case["id"]
    match check:
        case "parity":
            assert results["js"][cid] == results["python"][cid]
        case "honesty":
            verdict = results["js"][cid]
            assert verdict["action"] == "subset-exceeded"
            assert "capt-hook test" in (verdict["message"] or "")
        case "canned":
            assert results["python"][cid] == case["verdict"]
