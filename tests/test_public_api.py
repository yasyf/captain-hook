"""Public-API parity gate for the Phase-1 performance work (baseline commit ee9f7ca3).

The Phase-1 diet lazily re-exports the root package (PEP 562), defers the transcript
parse behind a proxy, and drops pydantic from the inline-test ``Input``. None of that
may change or degrade the public surface. This module pins:

* the exact set of root exports, derived from the pre-diet ``captain_hook/__init__.py``
  import block, each still ``is``-identical to the object in its defining module;
* the submodule import paths real consumers use;
* the behavioural contract of ``Input`` (validation, coercion, identity, immutability);
* the lazy-transcript proxy's equivalence to an eager ``Session`` and its deferred parse.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import captain_hook
from captain_hook import EXPORTS
from captain_hook.testing.types import FileFixture, Input, TranscriptFixture
from captain_hook.transcripts import lazy_transcript, load_transcript

ROOT_STUB = Path(captain_hook.__file__).with_suffix(".pyi")
MODULE_EXPORTS = frozenset({"file", "style", "util"})

SPAWNLLM_HOT_PATH_PROBE = """
import sys

import captain_hook

captain_hook.gate
captain_hook.nudge
loaded = sorted(name for name in sys.modules if name == "spawnllm" or name.startswith("spawnllm."))
assert not loaded, loaded
"""

# The root-package export surface pinned to baseline ee9f7ca3 — the sorted set of names
# bound at module level by that revision's captain_hook/__init__.py import block.
PINNED_EXPORTS: tuple[str, ...] = (
    "Action",
    "AfterEdit",
    "Agent",
    "Allow",
    "And",
    "Artifact",
    "Ask",
    "BackgroundTask",
    "BaseHookEvent",
    "BashCall",
    "BeforeEdit",
    "Block",
    "COMMENT_TYPES",
    "Clause",
    "Command",
    "CommandLine",
    "Commits",
    "Content",
    "CustomCommandLineCondition",
    "CustomCondition",
    "CustomInputTypeCondition",
    "Deque",
    "DurableSlot",
    "DurableState",
    "DurableStore",
    "EditCall",
    "EditedSource",
    "Event",
    "Excerpts",
    "ExitPlanModeCall",
    "File",
    "FileFixture",
    "FilePath",
    "FreshSession",
    "FromSubagent",
    "FromTeammate",
    "GateVerdict",
    "GlobCall",
    "GrepCall",
    "Headless",
    "HookContext",
    "HookResponse",
    "HookResult",
    "HookState",
    "HooksSettings",
    "InPlanMode",
    "InlineTests",
    "Input",
    "Introduced",
    "MultiEditCall",
    "NlpSignal",
    "Not",
    "NotebookEditCall",
    "NotificationEvent",
    "NudgeVerdict",
    "Occurrence",
    "Or",
    "OtherCall",
    "Pattern",
    "PermissionRequestEvent",
    "Phrase",
    "PostToolUseEvent",
    "PostToolUseFailureEvent",
    "PreCompactEvent",
    "PreToolUseEvent",
    "PrimitiveState",
    "Prompt",
    "PromptCheckVerdict",
    "PromptContext",
    "RanCommand",
    "ReadCall",
    "ReadFile",
    "Redirect",
    "Redirects",
    "Rewrite",
    "RewritingExistingPlan",
    "Rewritten",
    "Runs",
    "SafetyVerdict",
    "ScratchPath",
    "SessionCron",
    "SessionEndEvent",
    "SessionSlot",
    "SessionStartEvent",
    "SessionStore",
    "Signal",
    "Signals",
    "SkillCall",
    "SkipPermissions",
    "SourceEdits",
    "Step",
    "StopEvent",
    "SubagentStartEvent",
    "SubagentStopEvent",
    "TCondition",
    "Task",
    "TaskCall",
    "TaskCreateCall",
    "TaskUpdateCall",
    "Tasks",
    "TestFile",
    "Tool",
    "ToolCall",
    "ToolCallBase",
    "ToolHookEvent",
    "ToolInput",
    "ToolRewriteEvent",
    "TouchedFile",
    "TranscriptFixture",
    "UsedSkill",
    "UserMessages",
    "UserPromptSubmitEvent",
    "Waiting",
    "WalkContext",
    "Warn",
    "Workflow",
    "WorkflowScript",
    "WorkflowScriptSource",
    "WorkflowState",
    "WriteCall",
    "apply_contexts",
    "approve",
    "binary_supports",
    "block_command",
    "build_settings",
    "categorize_files",
    "deny",
    "diff_lint",
    "excerpt_around",
    "file",
    "gate",
    "has_nominal_subject",
    "hook",
    "is_past_predicate",
    "lint",
    "llm_approve",
    "llm_evaluate",
    "llm_gate",
    "llm_nudge",
    "nudge",
    "on",
    "prompt_check",
    "read_json",
    "resolve_binary",
    "rewrite_code",
    "rewrite_command",
    "rewrite_command_occurrences",
    "session_state",
    "set_tool_input",
    "style",
    "text_matches",
    "util",
    "warn_command",
    "workflow",
    "workflow_opt_matches",
    "workflow_opt_values",
    "workflow_script_source",
    "workflow_state",
)


# Submodule import paths exactly as real consumers spell them (repo hooks, cc-skills
# bootstrap templates, downstream packs). Each entry: (module, names it must expose).
CONSUMER_IMPORT_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("captain_hook.prompt", ("Prompt",)),
    ("captain_hook.types", ("Command", "Agent", "Or")),
    ("captain_hook.style", ("StyleDiffRule", "StyleRule", "matchers", "styleguide")),
    ("captain_hook.settings", ("HooksSettings",)),
    ("captain_hook.file", ("File", "PathMatcher")),
    ("captain_hook.events", ("PostToolUseFailureEvent",)),
    ("captain_hook.command", ("Command", "CommandLine")),
    ("cc_transcript.command", ("Command", "CommandLine")),
    ("captain_hook.util.model_cache", ("ensure_spacy_model",)),
)


def stub_exports(path: Path) -> dict[str, str]:
    exports: dict[str, str] = {}
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.ImportFrom) or node.module is None or node.level:
            raise ValueError(f"unexpected export stub statement: {ast.dump(node)}")
        for alias in node.names:
            if alias.asname != alias.name:
                raise ValueError(f"export must use `X as X`: {ast.unparse(node)}")
            if alias.name in exports:
                raise ValueError(f"duplicate export: {alias.name}")
            exports[alias.name] = f"captain_hook.{alias.name}" if node.module == "captain_hook" else node.module
    return exports


def test_stub_matches_exports() -> None:
    assert stub_exports(ROOT_STUB) == EXPORTS


def test_export_table_matches_pinned_surface() -> None:
    assert tuple(sorted(EXPORTS)) == PINNED_EXPORTS


def test_star_import_binds_exactly_the_exports() -> None:
    # `from captain_hook import *` binds every name in `__all__` and nothing else; exec
    # injects only __builtins__ into a fresh namespace.
    ns: dict[str, object] = {}
    exec("from captain_hook import *", ns)  # noqa: S102
    assert set(ns) - {"__builtins__"} == set(PINNED_EXPORTS)


def test_all_is_introspectable() -> None:
    assert "__all__" in dir(captain_hook)
    assert captain_hook.__all__ == sorted(PINNED_EXPORTS)
    assert vars(captain_hook)["__all__"] is captain_hook.__all__


def test_dir_lists_the_pinned_surface() -> None:
    listed = dir(captain_hook)
    assert set(PINNED_EXPORTS) <= set(listed)
    assert listed == sorted(listed)


@pytest.mark.parametrize("name", PINNED_EXPORTS, ids=PINNED_EXPORTS)
def test_pinned_name_imports_and_is_listed(name: str) -> None:
    obj = getattr(captain_hook, name)
    assert name in dir(captain_hook)
    assert obj is getattr(captain_hook, name)


@pytest.mark.parametrize("name", PINNED_EXPORTS, ids=PINNED_EXPORTS)
def test_pinned_name_is_object_from_defining_module(name: str) -> None:
    target = EXPORTS[name]
    defining = importlib.import_module(target)
    expected = defining if target == f"captain_hook.{name}" else getattr(defining, name)
    assert getattr(captain_hook, name) is expected


def test_only_pinned_facade_exports_resolve_to_modules() -> None:
    resolved_modules = {name for name in PINNED_EXPORTS if isinstance(getattr(captain_hook, name), ModuleType)}
    assert resolved_modules == MODULE_EXPORTS


def test_gate_and_nudge_do_not_load_spawnllm() -> None:
    out = subprocess.run([sys.executable, "-c", SPAWNLLM_HOT_PATH_PROBE], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'DefinitelyNotAThing'"):
        captain_hook.DefinitelyNotAThing  # noqa: B018


@pytest.mark.parametrize(
    ("module", "names"),
    [(m, n) for m, n in CONSUMER_IMPORT_PATHS if m != "captain_hook.command"],
    ids=[m for m, _ in CONSUMER_IMPORT_PATHS if m != "captain_hook.command"],
)
def test_consumer_import_path_resolves(module: str, names: tuple[str, ...]) -> None:
    imported = importlib.import_module(module)
    for name in names:
        assert getattr(imported, name) is not None


def test_captain_hook_command_module_absent_both_revisions() -> None:
    # `captain_hook.command` was folded into cc_transcript.command long before Phase 1
    # (commit 9c33ff50); it exists in neither baseline nor HEAD. Consumers reach Command
    # and CommandLine through the root package, captain_hook.types (Command only), or
    # cc_transcript.command. Pinned so a future re-add is a deliberate choice, not drift.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("captain_hook.command")
    assert captain_hook.Command is importlib.import_module("cc_transcript.command").Command
    assert captain_hook.CommandLine is importlib.import_module("cc_transcript.command").CommandLine


# --- Input behavioural parity (de-pydantic) --------------------------------------------

# Wrong-typed fields the pydantic baseline rejected and a plain dataclass would silently
# accept. Each must still raise a clear TypeError naming the offending field.
WRONG_TYPED: tuple[tuple[str, dict[str, Any]], ...] = (
    ("command_int", {"command": 123}),
    ("content_int", {"content": 5}),
    ("tool_str", {"tool": 7}),
    ("tool_input_list", {"tool_input": ["not", "a", "dict"]}),
    ("tool_input_str", {"tool_input": "nope"}),
    ("llm_list", {"llm": ["x"]}),
    ("tasks_dict", {"tasks": {"a": 1}}),
    ("tasks_str", {"tasks": "nope"}),
    ("file_int", {"file": 5}),
    ("model_int", {"model": 7}),
    ("agent_id_int", {"agent_id": 9}),
    ("offset_float", {"offset": 1.5}),
    ("offset_list", {"offset": [1]}),
    ("transcript_dict", {"transcript": {"a": 1}}),
    # Element/key checks: baseline pydantic rejected these; a plain dataclass whose
    # __post_init__ only checked the container type would silently accept them.
    ("tasks_bad_element", {"tasks": ["bad"]}),
    ("tool_input_int_key", {"tool_input": {1: "x"}}),
    ("llm_int_key", {"llm": {1: "x"}}),
    ("cwd_int", {"cwd": 5}),
)

VALID_FIELDS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("command", {"command": "ls"}),
    ("tool_input", {"tool_input": {"command": "ls"}}),
    ("offset_limit", {"offset": 10, "limit": 50}),
    ("skip_permissions", {"skip_permissions": True}),
    ("file_str", {"file": "x.py"}),
    ("file_fixture", {"file": FileFixture(size=10)}),
    ("tasks_list", {"tasks": [{"id": "1"}]}),
    ("llm_dict", {"llm": {"fire": False}}),
    ("cwd", {"cwd": "/x"}),
)


@pytest.mark.parametrize(("field", "kwargs"), WRONG_TYPED, ids=[c[0] for c in WRONG_TYPED])
def test_input_rejects_wrong_typed_field(field: str, kwargs: dict[str, Any]) -> None:
    key = next(iter(kwargs))
    with pytest.raises(TypeError, match=key):
        Input(**kwargs)


def test_input_rejects_unknown_keyword() -> None:
    with pytest.raises(TypeError, match="bogus"):
        Input(bogus=1)  # type: ignore[call-arg]


@pytest.mark.parametrize(("field", "kwargs"), VALID_FIELDS, ids=[c[0] for c in VALID_FIELDS])
def test_input_accepts_valid_field(field: str, kwargs: dict[str, Any]) -> None:
    assert isinstance(Input(**kwargs), Input)


def test_input_transcript_list_coerced_to_fixture() -> None:
    msgs = [{"type": "user", "message": {"content": "hi"}}]
    inp = Input(transcript=msgs)
    assert isinstance(inp.transcript, TranscriptFixture)
    assert inp.transcript.messages == msgs


def test_input_transcript_str_coerced_to_path() -> None:
    # Baseline's pydantic model coerced a str transcript to Path; the plain dataclass
    # must do the same, else input_to_event's `case Path()` drops the transcript.
    inp = Input(transcript="/tmp/x.jsonl")
    assert isinstance(inp.transcript, Path)
    assert inp.transcript == Path("/tmp/x.jsonl")


def test_input_str_transcript_is_loaded_end_to_end(tmp_path: Path) -> None:
    from captain_hook.types import Event
    from tests.helpers import input_to_event

    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type": "user", "uuid": "u", "sessionId": "s",'
        ' "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}}\n'
    )
    evt = input_to_event(Event.PreToolUse, Input(tool="Bash", command="ls", transcript=str(p)))
    assert len(evt.ctx.transcript) == 1


def test_input_dict_key_identity_semantics() -> None:
    # eq=False keeps identity hashing: two equal-valued Inputs stay distinct dict keys.
    keyed = {Input(command="x"): 1, Input(command="x"): 2}
    assert len(keyed) == 2
    assert Input(command="x") != Input(command="x")


def test_input_is_frozen() -> None:
    import dataclasses

    inp = Input(command="ls")
    with pytest.raises(dataclasses.FrozenInstanceError):
        inp.command = "rm"  # type: ignore[misc]


def test_input_repr_lists_only_set_fields() -> None:
    # Guards against internal machinery (e.g. a validation table) leaking into repr.
    assert repr(Input(command="ls", tool="Bash")) == "Input(command='ls', tool='Bash')"


# --- Lazy-transcript proxy parity ------------------------------------------------------

TRANSCRIPT_LINES = (
    '{"type": "user", "uuid": "u1", "sessionId": "s", "timestamp": "2026-01-01T00:00:00Z",'
    ' "message": {"content": "hello"}}\n'
    '{"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "s",'
    ' "timestamp": "2026-01-01T00:00:01Z",'
    ' "message": {"model": "claude-opus-4", "content": [{"type": "text", "text": "hi"}]}}\n'
)

# Session accessors that conditions.py and context.py invoke on ctx.transcript.
ACCESSORS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("truthiness", bool),
    ("len", len),
    ("events_count", lambda t: len(t.events)),
    ("path", lambda t: str(t.path)),
    ("tool_calls_count", lambda t: t.tool_calls.count()),
    ("edit_calls", lambda t: t.tool_calls.named("Edit|Write").count()),
    ("exit_plan_mode", lambda t: t.tool_calls.named("ExitPlanMode").count()),
    ("has_edit_to_py", lambda t: t.has_edit_to("**/*.py")),
    ("has_command_git", lambda t: t.has_command("git")),
    ("assistant_text", lambda t: t.assistant_text(n=10)),
    ("current_turn_len", lambda t: len(t.current_turn)),
    ("prior_len", lambda t: len(t.prior())),
    ("recent_len", lambda t: len(t.recent(5))),
)


@pytest.fixture
def transcript_path(tmp_path: Path) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text(TRANSCRIPT_LINES)
    return p


def test_proxy_is_a_session_instance(transcript_path: Path) -> None:
    from cc_transcript.query import Session

    proxy = lazy_transcript(transcript_path)
    assert isinstance(proxy, Session)
    assert proxy.__class__ is Session


@pytest.mark.parametrize("accessor", ACCESSORS, ids=[a[0] for a in ACCESSORS])
def test_proxy_matches_eager_session(transcript_path: Path, accessor: tuple[str, Callable[[Any], Any]]) -> None:
    _, fn = accessor
    proxy = lazy_transcript(transcript_path)
    eager = load_transcript(transcript_path)
    assert fn(proxy) == fn(eager)


def test_parse_is_deferred_until_first_touch(transcript_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cc_transcript import parser

    calls = {"n": 0}
    real = parser.parse_events_from_bytes

    def counting(data: bytes) -> Any:
        calls["n"] += 1
        return real(data)

    monkeypatch.setattr(parser, "parse_events_from_bytes", counting)
    proxy = lazy_transcript(transcript_path)
    assert calls["n"] == 0, "parse ran before the proxy was touched"
    assert bool(proxy) is True
    assert calls["n"] == 1, "first touch did not parse exactly once"
    assert len(proxy) == 2
    assert calls["n"] == 1, "a second touch re-parsed the transcript"
