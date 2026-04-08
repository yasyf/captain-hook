from __future__ import annotations

import ast
import logging
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import (
    _state,
    hook as register_hook,
    on,
    register,
    reset,
)
from captain_hook.dispatch import dispatch
from captain_hook.events import PostToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.types import (
    Event,
    FilePath,
    TestFile,
    Tool,
)


def make_ctx(session_dir: Path | None = None, transcript_len: int = 10) -> MagicMock:
    ctx = MagicMock()
    ctx.transcript = MagicMock()
    ctx.transcript.__len__ = MagicMock(return_value=transcript_len)
    ctx.transcript.has_skill = MagicMock(return_value=False)
    ctx.transcript.has_read = MagicMock(return_value=False)
    ctx.transcript.has_edit_to = MagicMock(return_value=False)
    ctx.transcript.has_command = MagicMock(return_value=False)
    ctx.transcript.count_tools = MagicMock(return_value=0)
    ctx.t = ctx.transcript
    store = SessionStore(session_dir)
    ctx.session = store
    ctx.s = store
    turn = MagicMock()
    turn.start_idx = 5
    ctx.turn = turn
    return ctx


@pytest.fixture
def work_dir():
    d = Path(tempfile.mkdtemp(prefix="src_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def session_dir():
    d = Path(tempfile.mkdtemp(prefix="session_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_post_tool_event(
    tool_name: str = "Edit",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def register_lint(
        check: Any,
    *,
    message: str = "Violations: {violations}",
    trigger: str | None = None,
    sep: str = ", ",
    block: bool = False,
    events: Event | None = None,
    tests: Any = None,
    max_shown: int = 5,
) -> None:
    from captain_hook.primitives.lint import lint

    lint(
        check,
        message=message,
        trigger=trigger,
        sep=sep,
        block=block,
        events=events,
        tests=tests,
        max_shown=max_shown,
    )
# --- VAL-LINT-001: String-mode lint ---

@pytest.fixture(autouse=True)
def _clean_state():
    reset()
    yield
    reset()



class TestStringModeLint:
    def test_string_check_receives_content(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["found_issue"] if "bad_pattern" in content else []

        register_lint(check, message="Issues: {violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "bad_pattern here"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert "found_issue" in result["hookSpecificOutput"]["additionalContext"]

    def test_string_check_no_violations_returns_none(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return []

        register_lint(check, message="Issues: {violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "clean content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None


# --- VAL-LINT-002: AST-mode lint ---


class TestAstModeLint:
    def test_ast_check_receives_tree(self, work_dir: Path, session_dir: Path) -> None:
        source = "import pdb\nx = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "pdb":
                            yield "pdb import found"

        register_lint(check, message="AST issues: {violations}", trigger="pdb")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "import pdb"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert "pdb import found" in result["hookSpecificOutput"]["additionalContext"]


# --- VAL-LINT-003: Mode detection via type hints ---


class TestModeDetection:
    def test_string_mode_detected_from_hint(self, session_dir: Path) -> None:
        called_with_str = False

        def check(content: str) -> list[str]:
            nonlocal called_with_str
            called_with_str = True
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "something"},
            ctx=make_ctx(session_dir),
        )
        dispatch(Event.PostToolUse, evt, session_dir)
        assert called_with_str

    def test_ast_mode_detected_from_hint(self, work_dir: Path, session_dir: Path) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)
        called_with_ast = False

        def check(tree: ast.AST) -> Iterator[str]:
            nonlocal called_with_ast
            called_with_ast = True
            return iter([])

        register_lint(check, message="{violations}", trigger="x")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            ctx=make_ctx(session_dir),
        )
        dispatch(Event.PostToolUse, evt, session_dir)
        assert called_with_ast


# --- VAL-LINT-004: Violations formatted with template and sep ---


class TestViolationFormatting:
    def test_violations_joined_with_sep(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["issue_a", "issue_b", "issue_c"]

        register_lint(check, message="Found: {violations}", sep="; ")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "issue_a; issue_b; issue_c" in msg
        assert "Found:" in msg

    def test_default_sep_is_comma_space(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["a", "b"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert "a, b" in result["hookSpecificOutput"]["additionalContext"]


# --- VAL-LINT-005: trigger short-circuits AST parsing ---


class TestTriggerShortCircuit:
    def test_trigger_absent_skips_ast_check(self, work_dir: Path, session_dir: Path) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)
        check_called = False

        def check(tree: ast.AST) -> Iterator[str]:
            nonlocal check_called
            check_called = True
            yield "should not appear"

        register_lint(check, message="{violations}", trigger="pdb")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None
        assert not check_called

    def test_trigger_present_runs_ast_check(self, work_dir: Path, session_dir: Path) -> None:
        source = "import pdb\nx = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)
        check_called = False

        def check(tree: ast.AST) -> Iterator[str]:
            nonlocal check_called
            check_called = True
            yield "pdb found"

        register_lint(check, message="{violations}", trigger="pdb")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "import pdb"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert check_called


# --- VAL-LINT-006: max_shown limits ---


class TestMaxShown:
    def test_max_shown_limits_violations(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return [f"v{i}" for i in range(10)]

        register_lint(check, message="{violations}", max_shown=3)
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "v0" in msg
        assert "v1" in msg
        assert "v2" in msg
        assert "v3" not in msg

    def test_default_max_shown_is_5(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return [f"v{i}" for i in range(10)]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "v4" in msg
        assert "v5" not in msg


# --- VAL-LINT-007: block=True ---


class TestBlockMode:
    def test_block_true_returns_deny(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}", block=True)
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_default_warns(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert "additionalContext" in result["hookSpecificOutput"]


# --- VAL-LINT-008: Default conditions ---


class TestDefaultConditions:
    def test_non_python_file_skipped(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "style.css", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None

    def test_test_file_skipped(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "tests/test_foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None

    def test_bash_tool_skipped(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Bash",
            tool_input={"command": "echo hello"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None

    def test_python_edit_matches(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None

    def test_write_tool_matches(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Write",
            tool_input={"file_path": "foo.py", "content": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None

    def test_spec_has_correct_defaults(self) -> None:

        def check(content: str) -> list[str]:
            return []

        register_lint(check, message="{violations}")
        assert len(_state.hooks) == 1
        spec = _state.hooks[0].spec
        assert spec.events == Event.PostToolUse
        assert any(isinstance(c, Tool) and c.pattern == "Edit|Write" for c in spec.only_if)
        assert any(isinstance(c, FilePath) and "*.py" in c.patterns for c in spec.only_if)
        assert any(isinstance(c, TestFile) for c in spec.skip_if)


# --- VAL-LINT-009: Reads from file or content ---


class TestFileVsContentRead:
    def test_string_mode_uses_evt_content(self, session_dir: Path) -> None:
        received_content = None

        def check(content: str) -> list[str]:
            nonlocal received_content
            received_content = content
            return ["v"] if "target" in content else []

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "target code"},
            ctx=make_ctx(session_dir),
        )
        dispatch(Event.PostToolUse, evt, session_dir)
        assert received_content == "target code"

    def test_ast_mode_reads_full_file_from_disk(self, work_dir: Path, session_dir: Path) -> None:
        full_source = "import pdb\ndef foo():\n    return 42\n"
        py_file = work_dir / "code.py"
        py_file.write_text(full_source)
        tree_nodes_seen: list[str] = []

        def check(tree: ast.AST) -> Iterator[str]:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        tree_nodes_seen.append(alias.name)
                        yield f"import {alias.name}"
                if isinstance(node, ast.FunctionDef):
                    tree_nodes_seen.append(node.name)

        register_lint(check, message="{violations}", trigger="pdb")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "import pdb"},
            ctx=make_ctx(session_dir),
        )
        dispatch(Event.PostToolUse, evt, session_dir)
        assert "pdb" in tree_nodes_seen
        assert "foo" in tree_nodes_seen


# --- VAL-LINT-010: SyntaxError returns None ---


class TestSyntaxError:
    def test_syntax_error_returns_none(self, work_dir: Path, session_dir: Path) -> None:
        source = "def foo(:\n    pass\n"
        py_file = work_dir / "bad.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            yield "should not appear"

        register_lint(check, message="{violations}", trigger="def")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "def foo(:"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None


# --- VAL-LINT-011: Empty violations don't fire ---


class TestEmptyViolations:
    def test_empty_list_returns_none(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return []

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "clean"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None

    def test_empty_iterator_returns_none(self, work_dir: Path, session_dir: Path) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            return iter([])

        register_lint(check, message="{violations}", trigger="x")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None


# --- VAL-LINT-012: @overload typing ---


class TestOverloadTyping:
    def test_both_modes_accepted(self) -> None:
        from captain_hook.primitives.lint import lint

        def str_check(content: str) -> list[str]:
            return []

        def ast_check(tree: ast.AST) -> Iterator[str]:
            return iter([])

        lint(str_check, message="{violations}")
        lint(ast_check, message="{violations}")
        assert len(_state.hooks) == 2


# --- VAL-LINT-013: trigger ignored in string mode ---


class TestTriggerIgnoredInStringMode:
    def test_trigger_absent_still_runs_string_check(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["found"]

        register_lint(check, message="{violations}", trigger="xyz")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content without trigger"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert "found" in result["hookSpecificOutput"]["additionalContext"]


# --- VAL-LINT-014: check function raises returns None ---


class TestCheckRaises:
    def test_string_check_raises_returns_none(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            raise ValueError("boom")

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None

    def test_ast_check_raises_returns_none(self, work_dir: Path, session_dir: Path) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            raise TypeError("oops")

        register_lint(check, message="{violations}", trigger="x")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None


# --- VAL-LINT-015: default events is PostToolUse ---


class TestDefaultEvents:
    def test_default_event_is_post_tool_use(self) -> None:

        def check(content: str) -> list[str]:
            return []

        register_lint(check, message="{violations}")
        assert _state.hooks[-1].spec.events == Event.PostToolUse

    def test_events_can_be_overridden(self) -> None:

        def check(content: str) -> list[str]:
            return []

        register_lint(check, message="{violations}", events=Event.PreToolUse)
        assert _state.hooks[-1].spec.events == Event.PreToolUse


# --- VAL-LINT-016: AST mode reads full file ---


class TestAstFullFileRead:
    def test_ast_parses_full_file_not_just_content(self, work_dir: Path, session_dir: Path) -> None:
        full_source = "class Foo:\n    pass\n\ndef bar():\n    return 1\n"
        py_file = work_dir / "module.py"
        py_file.write_text(full_source)
        found_nodes: list[str] = []

        def check(tree: ast.AST) -> Iterator[str]:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    found_nodes.append(f"class:{node.name}")
                    yield f"class {node.name}"
                if isinstance(node, ast.FunctionDef):
                    found_nodes.append(f"func:{node.name}")

        register_lint(check, message="{violations}", trigger="class")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "class Foo"},
            ctx=make_ctx(session_dir),
        )
        dispatch(Event.PostToolUse, evt, session_dir)
        assert "class:Foo" in found_nodes
        assert "func:bar" in found_nodes


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: lint() passes empty string through to checker (only skips None)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmptyStringPassthrough:
    def test_empty_string_content_reaches_checker(self, session_dir: Path) -> None:
        received_content: list[str | None] = []

        def check(content: str) -> list[str]:
            received_content.append(content)
            return ["empty file edit"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "old", "new_string": ""},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert len(received_content) == 1
        assert received_content[0] == ""
        assert result is not None

    def test_none_content_still_skips(self, session_dir: Path) -> None:
        check_called = False

        def check(content: str) -> list[str]:
            nonlocal check_called
            check_called = True
            return ["should not fire"]

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert not check_called
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: lint() logs check function exceptions instead of silently swallowing
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: AST lint passes empty .py file through ast.parse('') instead of
# skipping it. Only None (missing source) should be skipped.
# ═══════════════════════════════════════════════════════════════════════════════


class TestAstLintEmptyFile:
    def test_empty_py_file_runs_ast_check(self, work_dir: Path, session_dir: Path) -> None:
        py_file = work_dir / "empty.py"
        py_file.write_text("")
        received_tree: list[ast.AST] = []

        def check(tree: ast.AST) -> Iterator[str]:
            received_tree.append(tree)
            return iter([])

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": ""},
            ctx=make_ctx(session_dir),
        )
        dispatch(Event.PostToolUse, evt, session_dir)
        assert len(received_tree) == 1
        assert isinstance(received_tree[0], ast.Module)

    def test_empty_py_file_with_violations_fires(self, work_dir: Path, session_dir: Path) -> None:
        py_file = work_dir / "empty.py"
        py_file.write_text("")

        def check(tree: ast.AST) -> Iterator[str]:
            if not list(ast.walk(tree)):
                yield "empty module"
            yield "always fires"

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": ""},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert "always fires" in result["hookSpecificOutput"]["additionalContext"]

    def test_none_source_still_skips_ast(self, session_dir: Path) -> None:
        from captain_hook.primitives.lint import run_ast_check

        check_called = False

        def check(tree: ast.AST) -> Iterator[str]:
            nonlocal check_called
            check_called = True
            yield "should not fire"

        evt = make_post_tool_event(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            ctx=make_ctx(session_dir),
        )
        result = run_ast_check(check, evt, "msg", None, ", ", False, 5)
        assert result is None
        assert not check_called


class TestCheckExceptionLogging:
    def test_string_check_exception_is_logged(self, session_dir: Path, caplog: pytest.LogCaptureFixture) -> None:

        def check(content: str) -> list[str]:
            raise ValueError("intentional boom")

        register_lint(check, message="{violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            ctx=make_ctx(session_dir),
        )
        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.lint"):
            result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None
        assert any("check" in r.message and r.exc_info for r in caplog.records)

    def test_ast_check_exception_is_logged(
        self,
        work_dir: Path,
        session_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            raise TypeError("ast boom")

        register_lint(check, message="{violations}", trigger="x")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            ctx=make_ctx(session_dir),
        )
        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.lint"):
            result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is None
        assert any("check" in r.message and r.exc_info for r in caplog.records)
