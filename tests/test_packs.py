from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

import captain_hook
from captain_hook import Prompt, app
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack, import_pack_module
from captain_hook.packs import manager
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.types import Event

BUILTIN_PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"
EXPECTED_BUILTINS = {"general", "python", "go", "steering", "fixes", "performance"}
UNCONDITIONAL = {"fixes", "general", "steering", "performance"}
GENERAL_HOOKS = {
    "commands",
    "comments",
    "deletions",
    "detours",
    "docs",
    "models",
    "plans",
    "prompts",
    "review",
    "tasks",
    "tombstones",
    "tools",
}
PYTHON_HOOKS = {"style", "testing", "toolchain"}
GO_HOOKS = {"testing", "toolchain"}
STEERING_HOOKS = {"steering", "teammates", "workarounds"}
FIXES_HOOKS = {"teammate_permissions", "scratch_writes"}
PERFORMANCE_HOOKS = {"pipelining"}
HOOK_SRC = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"
SRC_USES_FILE = (
    "from pathlib import Path\n"
    "from captain_hook import Event, hook\n"
    "_here = Path(__file__).parent\n"
    "hook(Event.PreToolUse, message=str(_here))\n"
)
WARNING_NO = logger.level("WARNING").no


# --- builtin pack content -------------------------------------------------------------


def test_expected_builtin_packs_present() -> None:
    on_disk = {p.name for p in BUILTIN_PACKS_DIR.iterdir() if p.is_dir() and (p / manager.HOOKS_DIRNAME).is_dir()}
    assert on_disk == EXPECTED_BUILTINS
    assert set(manager.builtin_names()) == EXPECTED_BUILTINS


@pytest.mark.parametrize(
    ("name", "hook_stems"),
    [
        ("general", GENERAL_HOOKS),
        ("python", PYTHON_HOOKS),
        ("go", GO_HOOKS),
        ("steering", STEERING_HOOKS),
        ("fixes", FIXES_HOOKS),
        ("performance", PERFORMANCE_HOOKS),
    ],
    ids=["general", "python", "go", "steering", "fixes", "performance"],
)
def test_builtin_pack_layout(name: str, hook_stems: set[str]) -> None:
    resolved = manager.resolve_builtin(name)
    assert resolved.entry == manager.BuiltinPack(name)
    assert resolved.path == BUILTIN_PACKS_DIR / name / "hooks"
    assert {p.stem for p in resolved.path.glob("*.py") if not p.stem.startswith("_")} == hook_stems


def test_builtin_pack_ids_are_namespaced() -> None:
    assert manager.resolve_builtin("general").pack_id == "builtin:general"


@pytest.mark.parametrize("name", ["general", "steering"])
def test_nlp_builtins_declare_resources(name: str) -> None:
    assert manager.resolve_builtin(name).descriptor.resources == ("spacy:en_core_web_sm", "wordnet:oewn:2025")


@pytest.mark.parametrize("name", ["fixes", "go", "python", "performance"])
def test_plain_builtins_have_empty_descriptor(name: str) -> None:
    descriptor = manager.resolve_builtin(name).descriptor
    assert descriptor == manager.PackDescriptor()
    assert not (BUILTIN_PACKS_DIR / name / manager.PACK_DESCRIPTOR).exists()


@pytest.mark.parametrize(
    "name",
    [
        "fragments/deliverable_rubric.md",
        "fragments/workflow_script_header.md",
        "models/prose_spawn_gate.md",
        "models/implementation_spawn_nudge.md",
        "models/inline_edit_nudge.md",
        "tools/preload_nudge.md",
    ],
)
def test_general_pack_prompts_are_packaged(name: str) -> None:
    """The general pack's Prompt.load .md files must ship as package data (wheel/plugin)."""
    assert (resources.files(captain_hook) / "builtin_packs/general/hooks/prompts" / name).is_file()


def test_fixes_pack_approves_teammate_tools(isolate_modules: None, tmp_path: Path) -> None:
    discover_pack("fixes", manager.resolve_builtin("fixes").path)

    def decision(tool: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
        evt = input_to_event(
            Event.PermissionRequest,
            Input(tool=tool, tool_input=tool_input, agent_id="tm1", skip_permissions=True),
        )
        return dispatch(Event.PermissionRequest, evt, session_dir=tmp_path)

    def behavior(result: dict[str, Any] | None) -> str | None:
        return result["hookSpecificOutput"]["decision"]["behavior"] if result else None

    assert behavior(decision("Bash", {"command": "echo hi"})) == "allow"
    assert behavior(decision("mcp__srv__Bash", {"command": "echo hi"})) == "allow"
    assert behavior(decision("mcp__plugin_cc-notes_cc-notes__doc_search", {"query": "F1"})) == "allow"
    assert behavior(decision("WebFetch", {"url": "https://example.com"})) == "allow"
    assert decision("mcp__ops__Bash", {"command": "rm -rf /"}) is None  # command denylist rides along
    assert decision("mcp__ops__delete_everything", {}) is None  # destructive verb token
    assert decision("Bash", {"command": "rm -rf build"}) is None


def test_fixes_pack_approves_scratch_writes(isolate_modules: None, tmp_path: Path) -> None:
    discover_pack("fixes", manager.resolve_builtin("fixes").path)

    def decision(tool: str, tool_input: dict[str, Any], cwd: str | None = None) -> dict[str, Any] | None:
        evt = input_to_event(
            Event.PermissionRequest,
            Input(tool=tool, tool_input=tool_input, cwd=cwd, skip_permissions=True),
        )
        return dispatch(Event.PermissionRequest, evt, session_dir=tmp_path)

    allowed = decision("Write", {"file_path": "/tmp/sweep_arc.py", "content": "print(1)"})
    assert allowed is not None
    assert allowed["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    relative = decision("Write", {"file_path": "../../../../tmp/x.py", "content": "x"}, cwd="/a/b/c/d")
    assert relative is not None
    assert relative["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert decision("Write", {"file_path": "/Users/u/proj/src/main.py", "content": "x"}) is None
    assert decision("Write", {"file_path": "/tmp/../Users/u/proj/main.py", "content": "x"}) is None
    assert decision("mcp__srv__Write", {"file_path": "/tmp/x.py", "content": "x"}) is None


def test_general_pack_preload_tools_nudge(isolate_modules: None, tmp_path: Path) -> None:
    general = manager.resolve_builtin("general")
    discover_pack("general", general.path)
    select = (
        "select:TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,"
        "Monitor,SendMessage,EnterPlanMode,ExitPlanMode"
    )
    rendered = str(Prompt.load("tools/preload_nudge", base=general.path / "prompts"))

    def context(source: str, *, agent_id: str | None = None) -> str | None:
        evt = input_to_event(Event.SessionStart, Input(source=source, agent_id=agent_id))
        out = dispatch(Event.SessionStart, evt, session_dir=tmp_path / f"{source}-{agent_id or 'main'}")
        return out["hookSpecificOutput"]["additionalContext"] if out else None

    injected = context("startup")
    assert injected is not None and rendered in injected
    cleared = context("clear")
    assert cleared is not None and select in cleared
    assert context("resume") is None
    assert context("compact") is None
    assert context("startup", agent_id="sub-1") is None
    assert context("clear", agent_id="sub-1") is None
    # max_fires=1: a repeat startup in the same session dir never re-injects
    assert context("startup") is None


# --- PackDescriptor -------------------------------------------------------------------


def test_descriptor_absent_is_empty(tmp_path: Path) -> None:
    assert manager.PackDescriptor.load(tmp_path / "pack.toml") == manager.PackDescriptor()


def test_descriptor_parses_resources_and_tools(tmp_path: Path) -> None:
    (path := tmp_path / "pack.toml").write_text(
        'resources = ["spacy:en_core_web_sm"]\n\n'
        '[tools.ccx_code_edit]\n'
        'behaves_like = "Edit"\n'
        'span_edit = { path = "path", content = "content", delete = "delete" }\n\n'
        '[tools.BashFormat]\n'
        'behaves_like = "Bash"\n'
    )
    descriptor = manager.PackDescriptor.load(path)
    assert descriptor.resources == ("spacy:en_core_web_sm",)
    # Tool keys are the bare tool segment, not a full mcp__server__ name: one key matches ccx_code_edit
    # under any plugin/server mount prefix.
    edit, fmt = descriptor.tools
    assert edit == manager.ToolSpec("ccx_code_edit", "Edit", manager.SpanEditSpec("path", "content", "delete"))
    assert fmt == manager.ToolSpec("BashFormat", "Bash")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('resources = "not-a-list"\n', id="resources_not_list"),
        pytest.param("resources = [1]\n", id="resource_not_string"),
        pytest.param('[tools.x]\nspan_edit = { path = "p", content = "c" }\n', id="tool_missing_behaves_like"),
        pytest.param('[tools.x]\nbehaves_like = "Edit"\nspan_edit = "nope"\n', id="span_edit_not_table"),
    ],
)
def test_descriptor_rejects_malformed(tmp_path: Path, body: str) -> None:
    (path := tmp_path / "pack.toml").write_text(body)
    with pytest.raises(manager.PackError):
        manager.PackDescriptor.load(path)


# --- builtin activation ---------------------------------------------------------------


def test_active_builtins_unconditional_only(tmp_path: Path) -> None:
    assert set(manager.active_builtins(tmp_path)) == UNCONDITIONAL


@pytest.mark.parametrize(
    ("marker", "language"),
    [("go.mod", "go"), ("go.work", "go"), ("pyproject.toml", "python")],
)
def test_active_builtins_detects_language_at_root(tmp_path: Path, marker: str, language: str) -> None:
    (tmp_path / marker).write_text("")
    assert set(manager.active_builtins(tmp_path)) == UNCONDITIONAL | {language}


def test_active_builtins_detects_nested_module(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "go.mod").write_text("module x\n")
    assert "go" in manager.active_builtins(tmp_path)


def test_detect_languages_ignores_gitignored_marker(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("vendor/\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "go.mod").write_text("module x\n")
    assert manager.detect_languages(tmp_path) == frozenset()


def test_detect_languages_skips_vcs_dir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "pyproject.toml").write_text("")
    assert manager.detect_languages(tmp_path) == frozenset()


def test_detect_languages_finds_both(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "pyproject.toml").write_text("")
    assert manager.detect_languages(tmp_path) == frozenset({"go", "python"})


def test_resolve_builtin_unknown_fails_loud() -> None:
    with pytest.raises(manager.PackError):
        manager.resolve_builtin("nope")


@pytest.mark.parametrize(
    ("name", "module"),
    [("fixes", "fixes"), ("cc-context@cc-context", "cc_context_cc_context"), ("a.b@c", "a_b_c")],
)
def test_pack_module_name(name: str, module: str) -> None:
    assert manager.pack_module_name(name) == module


# --- loader.discover_pack ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stems", "present", "absent"),
    [
        pytest.param(("hook", "_skip", "conf"), ("hook",), (), id="skips_underscore_and_conf"),
        pytest.param(("hook", "test_hook", "conftest"), ("hook",), ("test_hook", "conftest"), id="skips_test_files"),
    ],
)
def test_discover_pack_skips(
    tmp_path: Path, stems: tuple[str, ...], present: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    for stem in stems:
        (pack / f"{stem}.py").write_text(HOOK_SRC)
    discover_pack("solo", pack)
    assert len(app._state.hooks) == 1
    for module in present:
        assert f"captain_hook._packs.solo.{module}" in sys.modules
    for module in absent:
        assert f"captain_hook._packs.solo.{module}" not in sys.modules


def test_discover_pack_namespaces_avoid_collision(tmp_path: Path) -> None:
    for name in ("a", "b"):
        pack = tmp_path / name
        pack.mkdir()
        (pack / "commands.py").write_text(HOOK_SRC)
        discover_pack(name, pack)
    assert len(app._state.hooks) == 2
    assert "captain_hook._packs.a.commands" in sys.modules
    assert "captain_hook._packs.b.commands" in sys.modules


def test_discover_pack_sanitizes_name(tmp_path: Path) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "h.py").write_text(HOOK_SRC)
    discover_pack("cc-context@cc-context", pack)
    assert "captain_hook._packs.cc_context_cc_context.h" in sys.modules


def test_discover_pack_skips_marked_library_but_keeps_it_importable(tmp_path: Path, isolate_modules: None) -> None:
    pack = tmp_path / "marked"
    pack.mkdir()
    (pack / "hook.py").write_text(HOOK_SRC)
    # A non-underscore library file the loader must skip on the strength of the marker
    # alone (it would otherwise be auto-loaded). It still imports cleanly on demand.
    (pack / "lib.py").write_text("__capt_hook_skip__ = True\nSHARED = 42\n")
    discover_pack("ccx-marked", pack)

    assert len(app._state.hooks) == 1  # only hook.py registered; the marked lib stayed inert
    assert "captain_hook._packs.ccx_marked.hook" in sys.modules
    assert "captain_hook._packs.ccx_marked.lib" not in sys.modules  # the auto-load loop never imported it

    lib = __import__("captain_hook._packs.ccx_marked.lib", fromlist=["SHARED"])  # but it remains importable
    assert lib.SHARED == 42
    assert len(app._state.hooks) == 1  # importing the marked library registers nothing


def test_discover_pack_warns_and_continues_on_unloadable(tmp_path: Path, logcap: Any) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "good.py").write_text(HOOK_SRC)
    (pack / "bad.py").write_text("raise RuntimeError('boom')\n")
    discover_pack("solo", pack)
    assert len(app._state.hooks) == 1
    assert "captain_hook._packs.solo.good" in sys.modules
    assert "captain_hook._packs.solo.bad" not in sys.modules
    assert any("bad.py" in r.message and r.levelno >= WARNING_NO for r in logcap.records)


def test_import_pack_module_sets_file(tmp_path: Path) -> None:
    path = tmp_path / "uses_file.py"
    path.write_text(SRC_USES_FILE)
    module = import_pack_module("captain_hook._packs.solo.uses_file", path)
    assert module.__file__ == str(path)
    assert len(app._state.hooks) == 1


# --- steering pack: enabled registration --------------------------------------------


def test_enabling_steering_pack_registers_hooks() -> None:
    resolved = manager.resolve_builtin("steering")
    discover_pack("steering", resolved.path)
    # Seven: steering.py's two signal nudges + band-aid nudge + deferral gate, workarounds.py's
    # nudge + gate, teammates.py's digest nudge.
    assert len(app._state.hooks) == 7
    assert "captain_hook._packs.steering.steering" in sys.modules
    assert "captain_hook._packs.steering.teammates" in sys.modules


def test_steering_deferral_gate_skips_in_plan_mode() -> None:
    from captain_hook.types import InPlanMode, Waiting

    discover_pack("steering", manager.resolve_builtin("steering").path)
    # Of the two Stop-gates (deferral + upstream-workaround), the deferral gate is the one that
    # skips in plan mode; the workaround gate guards only on Waiting().
    (gate,) = (
        h
        for h in app._state.hooks
        if h.spec.events & (Event.Stop | Event.SubagentStop) and InPlanMode() in h.spec.skip_if
    )
    assert gate.spec.skip_if == (Waiting(), InPlanMode())
