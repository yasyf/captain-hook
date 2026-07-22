from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
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
COMPILER_TESTS_MJS = SRC / "tests" / "compiler.test.mjs"
RM_WORLD_TESTS_MJS = SRC / "tests" / "rm_world.test.mjs"
MATRIX = json.loads((SRC / "matrix.json").read_text())

sys.path.insert(0, str(SCRIPTS))

from build_emulator import BANNER_PREFIX, BUNDLES, src_hash  # noqa: E402
from widget_compiler import compile_fragment, load_hooks  # noqa: E402

import captain_hook  # noqa: E402
from captain_hook.app import _state  # noqa: E402
from captain_hook.dispatch import dispatch  # noqa: E402
from captain_hook.loader import discover_pack  # noqa: E402
from captain_hook.testing.helpers import input_to_event, isolated_state_root  # noqa: E402
from captain_hook.testing.types import Input  # noqa: E402
from captain_hook.types import Event  # noqa: E402
from captain_hook.util.scratch import is_scratch_path  # noqa: E402
from captain_hook.util.vcs import in_vcs_repo  # noqa: E402

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


def run_node_compile(source: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["node", str(PARITY_MJS)],
        input=json.dumps({"mode": "compile", "source": source}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def run_node_world(world: dict[str, Any], command: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["node", str(PARITY_MJS)],
        input=json.dumps({"mode": "world", "world": world, "input": {"command": command}}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)["verdict"]


def materialize_world(world: dict[str, Any], base: Path) -> Path:
    """Realize a WorldSpec under ``base`` for the real engine to walk: the declared files, empty dirs
    (a trailing ``/``), and a ``git init`` per repo, mapping the world's absolute cwd to
    ``base / <cwd without leading />``. Messages stay byte-equal because targets are world-relative."""
    cwd = base / world["cwd"].lstrip("/")
    cwd.mkdir(parents=True, exist_ok=True)
    for entry in world["files"]:
        if entry.endswith("/"):
            (cwd / entry).mkdir(parents=True, exist_ok=True)
        else:
            (target := cwd / entry).parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    for repo in world["repos"]:
        (repo_dir := cwd / repo).mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    return cwd


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


def _world_cases() -> list[Any]:
    """Every rm_walk case at the world's declared trash, plus the recoverable-rewrite case again with
    trash absent — the one case whose verdict flips (rewrite ↔ unrecoverable block) on trash presence."""
    widget = MATRIX["widgets"]["rm_walk"]
    trash = widget["world"]["trash"]
    return [pytest.param(c, trash, id=f"{c['id']}:trash") for c in widget["cases"]] + [
        pytest.param(c, None, id=f"{c['id']}:no-trash") for c in widget["cases"] if c["id"] == "recoverable-rewrite"
    ]


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda b: b.outfile.name)
def test_bundle_drift(bundle: Any) -> None:
    banner = bundle.outfile.read_text().splitlines()[0]
    assert banner == f"{BANNER_PREFIX}{src_hash()}", (
        f"committed {bundle.outfile.name} is stale — run `python docs/scripts/build_emulator.py`"
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


@pytest.fixture(scope="module")
def rm_world_cwd() -> Iterator[Path]:
    """Materialize the rm_walk world once under a non-scratch, non-repo base. Parity holds only there:
    pytest's ``tmp_path`` sits under a temp root, which would make every relative target scratch-exempt
    (breaking the rewrite/block cases), and any repo ancestor would make them all in-repo."""
    world = MATRIX["widgets"]["rm_walk"]["world"]
    (cache := Path.home() / ".cache").mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="capt-hook-world-", dir=cache))
    try:
        cwd = materialize_world(world, base)
        assert not is_scratch_path(cwd / "probe") and not in_vcs_repo(cwd), f"{base} is scratch or in a repo"
        yield cwd
    finally:
        shutil.rmtree(base, ignore_errors=True)


@requires_node
@pytest.mark.parametrize(("case", "trash"), _world_cases())
def test_rm_world_parity(
    case: dict[str, Any], trash: str | None, rm_world_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each rm_walk case is byte-equal between the real general-pack ``guard_rm`` — dispatched over a
    materialized tmpdir of the declared filesystem, with ``trash_binary`` pinned to the world's trash —
    and parity.mjs's world engine on the same declaration, across both trash-present and trash-absent."""
    world = MATRIX["widgets"]["rm_walk"]["world"] | {"trash": trash}
    saved = list(_state.hooks)
    try:
        _state.hooks.clear()
        discover_pack("general", PACKS_DIR / "general" / "hooks")
        deletions = sys.modules[next(h.handler.__module__ for h in _state.hooks if h.name == "guard_rm")]
        monkeypatch.setattr(deletions, "trash_binary", lambda: trash)
        with isolated_state_root():
            evt = input_to_event(Event.PreToolUse, Input(**case["input"], cwd=str(rm_world_cwd)))
            real = normalize(dispatch(Event.PreToolUse, evt))
    finally:
        _state.hooks[:] = saved
    assert real == run_node_world(world, case["input"]["command"])
    # Contract: world-relative / /tmp / / targets only, so no message embeds the materialized base.
    assert str(rm_world_cwd) not in json.dumps(real)


# Faithfully-modelable review fixes the declared cases miss: completed no-arg wrappers and `--`.
RM_WORLD_PARITY_EXTRAS = [
    "exec rm -rf /",
    "nice rm -rf /",
    "doas rm -rf /",
    "exec rm foo.txt",
    "nice rm foo.txt",
    "doas rm foo.txt",
    "env FOO=1 rm foo.txt",
    "rm -- -foo.txt",
    "rm -- data.txt",
    "rm foo.txt -- notes.md",
    "rm -- -r junk/a.log",
    "rm --",
]


@requires_node
@pytest.mark.parametrize("command", RM_WORLD_PARITY_EXTRAS)
def test_rm_world_parity_extras(command: str, rm_world_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte-parity between the real ``guard_rm`` and the world engine for the terminator and completed
    no-arg wrapper cases, at the world's declared trash — the axis the declared cases leave uncovered."""
    world = MATRIX["widgets"]["rm_walk"]["world"]
    trash = world["trash"]
    saved = list(_state.hooks)
    try:
        _state.hooks.clear()
        discover_pack("general", PACKS_DIR / "general" / "hooks")
        deletions = sys.modules[next(h.handler.__module__ for h in _state.hooks if h.name == "guard_rm")]
        monkeypatch.setattr(deletions, "trash_binary", lambda: trash)
        with isolated_state_root():
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(rm_world_cwd)))
            real = normalize(dispatch(Event.PreToolUse, evt))
    finally:
        _state.hooks[:] = saved
    assert real == run_node_world(world, command)
    assert str(rm_world_cwd) not in json.dumps(real)


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
    "named_escape": "from captain_hook import Event, hook\n"
    "hook(Event.PreToolUse, 'bullet \\N{BULLET} here', block=True)\n",
    "tuple_pattern": "from captain_hook import block_command\nblock_command(('git', 'push'), reason='x')\n",
    "truncated_hex": "from captain_hook import Event, hook\nhook(Event.PreToolUse, 'oops \\x1', block=True)\n",
    "not_arity": "from captain_hook import Event, hook\nfrom captain_hook.types import Command, Not\n"
    "hook(Event.PreToolUse, 'x', only_if=[Not(Command('a'), Command('b'))], block=True)\n",
    "unknown_kwarg": "from captain_hook import Event, hook\nhook(Event.PreToolUse, 'x', block=True, bogus=1)\n",
    "top_level_assign": "from captain_hook import Event, hook\nx = hook(Event.PreToolUse, 'x', block=True)\n",
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
            "advisory_on_deny": False,
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


FRAGMENT_WIDGETS = [
    pytest.param(widget["fragment"], id=widget_id)
    for widget_id, widget in MATRIX["widgets"].items()
    if "fragment" in widget
]


@requires_node
@pytest.mark.parametrize("fragment", FRAGMENT_WIDGETS)
def test_compile_parity(fragment: str) -> None:
    """Each matrix fragment lowers to identical hooks through the JS compiler bundle and Python."""
    saved = list(_state.hooks)
    try:
        python_hooks = compile_fragment(FRAGMENTS / f"{fragment}.py")["hooks"]
    finally:
        _state.hooks[:] = saved
    result = run_node_compile((FRAGMENTS / f"{fragment}.py").read_text())
    assert "error" not in result, result
    assert result["hooks"] == python_hooks


@requires_node
@pytest.mark.parametrize("source", REFUSALS.values(), ids=list(REFUSALS))
def test_compile_refusal_parity(source: str) -> None:
    """Every source the Python compiler refuses is refused by the JS bundle too (message-agnostic)."""
    result = run_node_compile(source)
    assert result.get("error"), result


@requires_node
def test_compiler_node_unit_suite() -> None:
    """Run the compiler.js `node --test` unit suite (refusals, kwarg ignoring, triple-quote lowering)."""
    proc = subprocess.run([NODE, "--test", str(COMPILER_TESTS_MJS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@requires_node
def test_rm_world_node_unit_suite() -> None:
    """Run the rm_walk world-engine `node --test` suite (redirect/wrapper/grouping/time/glob honesty)."""
    proc = subprocess.run([NODE, "--test", str(RM_WORLD_TESTS_MJS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@requires_node
def test_compile_parity_octal_and_concat(tmp_path: Path) -> None:
    """Octal escapes (in a message and a Command pattern) and adjacent-string concatenation lower
    to identical hooks through both compile_fragment and the JS compiler bundle."""
    source = (
        "from captain_hook import Event, hook\n"
        "from captain_hook.types import Command\n"
        "hook(\n"
        "    Event.PreToolUse,\n"
        "    'a\\012b ' '\\101\\162',\n"
        "    only_if=[Command('\\162m' 'x\\012')],\n"
        "    block=True,\n"
        ")\n"
    )
    python_hooks = _compile_source(source, tmp_path)
    result = run_node_compile(source)
    assert "error" not in result, result
    assert (
        result["hooks"]
        == python_hooks
        == [
            {
                "events": ["PreToolUse"],
                "message": "a\nb Ar",
                "block": True,
                "advisory_on_deny": False,
                "only_if": [{"kind": "Command", "pattern": "rmx\n"}],
                "skip_if": [],
            }
        ]
    )


def test_tool_aliases_in_sync() -> None:
    """The committed tool-alias map matches what the installed cc_transcript expands to, so a package
    drift fails here instead of shipping a stale bundle."""
    from build_emulator import tool_alias_map

    committed = json.loads((SRC / "generated" / "tool_aliases.json").read_text())
    assert committed == tool_alias_map()
