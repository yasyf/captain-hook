from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import _state
from captain_hook.dispatch import dispatch
from captain_hook.tests.helpers import make_ctx, make_post_tool_event
from captain_hook.types import (
    Event,
    FilePath,
    TestFile,
    Tool,
)


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


def lint_and_dispatch(
    session_dir: Path,
    check: Any,
    *,
    tool_name: str = "Edit",
    tool_input: dict[str, Any],
    **lint_kwargs: Any,
) -> Any:
    register_lint(check, **lint_kwargs)
    evt = make_post_tool_event(
        tool_name=tool_name,
        tool_input=tool_input,
        ctx=make_ctx(session_dir),
    )
    return dispatch(Event.PostToolUse, evt, session_dir)


class TestStringModeLint:
    def test_string_check_receives_content(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["found_issue"] if "bad_pattern" in content else []

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "bad_pattern here"},
            message="Issues: {violations}",
        )
        assert result is not None
        assert "found_issue" in result["hookSpecificOutput"]["additionalContext"]

    def test_string_check_no_violations_returns_none(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return []

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "clean content"},
            message="Issues: {violations}",
        )
        assert result is None


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

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "import pdb"},
            message="AST issues: {violations}",
            trigger="pdb",
        )
        assert result is not None
        assert "pdb import found" in result["hookSpecificOutput"]["additionalContext"]


class TestModeDetection:
    def test_string_mode_detected_from_hint(self, session_dir: Path) -> None:
        called_with_str = False

        def check(content: str) -> list[str]:
            nonlocal called_with_str
            called_with_str = True
            return ["violation"]

        lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "something"},
        )
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

        lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            trigger="x",
        )
        assert called_with_ast


class TestViolationFormatting:
    def test_violations_joined_with_sep(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["issue_a", "issue_b", "issue_c"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            message="Found: {violations}",
            sep="; ",
        )
        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "issue_a; issue_b; issue_c" in msg
        assert "Found:" in msg

    def test_default_sep_is_comma_space(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["a", "b"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
        )
        assert result is not None
        assert "a, b" in result["hookSpecificOutput"]["additionalContext"]


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

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            trigger="pdb",
        )
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

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "import pdb"},
            trigger="pdb",
        )
        assert result is not None
        assert check_called


class TestMaxShown:
    def test_max_shown_limits_violations(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return [f"v{i}" for i in range(10)]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            max_shown=3,
        )
        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "v0" in msg
        assert "v1" in msg
        assert "v2" in msg
        assert "v3" not in msg

    def test_default_max_shown_is_5(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return [f"v{i}" for i in range(10)]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
        )
        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "v4" in msg
        assert "v5" not in msg


class TestBlockMode:
    def test_block_true_returns_deny(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
            block=True,
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_default_warns(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
        )
        assert result is not None
        assert "additionalContext" in result["hookSpecificOutput"]


class TestDefaultConditions:
    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            pytest.param(
                "Edit",
                {"file_path": "style.css", "old_string": "", "new_string": "content"},
                id="non_python_file",
            ),
            pytest.param(
                "Edit",
                {"file_path": "tests/test_foo.py", "old_string": "", "new_string": "content"},
                id="test_file",
            ),
            pytest.param("Bash", {"command": "echo hello"}, id="bash_tool"),
        ],
    )
    def test_skipped(self, session_dir: Path, tool_name: str, tool_input: dict[str, Any]) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        assert lint_and_dispatch(session_dir, check, tool_name=tool_name, tool_input=tool_input) is None

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            pytest.param(
                "Edit",
                {"file_path": "foo.py", "old_string": "", "new_string": "content"},
                id="python_edit",
            ),
            pytest.param("Write", {"file_path": "foo.py", "content": "content"}, id="write_tool"),
        ],
    )
    def test_matches(self, session_dir: Path, tool_name: str, tool_input: dict[str, Any]) -> None:

        def check(content: str) -> list[str]:
            return ["violation"]

        assert lint_and_dispatch(session_dir, check, tool_name=tool_name, tool_input=tool_input) is not None

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


class TestFileVsContentRead:
    def test_string_mode_uses_evt_content(self, session_dir: Path) -> None:
        received_content = None

        def check(content: str) -> list[str]:
            nonlocal received_content
            received_content = content
            return ["v"] if "target" in content else []

        lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "target code"},
        )
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

        lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "import pdb"},
            trigger="pdb",
        )
        assert "pdb" in tree_nodes_seen
        assert "foo" in tree_nodes_seen


class TestSyntaxError:
    def test_syntax_error_returns_none(self, work_dir: Path, session_dir: Path) -> None:
        source = "def foo(:\n    pass\n"
        py_file = work_dir / "bad.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            yield "should not appear"

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "def foo(:"},
            trigger="def",
        )
        assert result is None


class TestEmptyViolations:
    def test_empty_list_returns_none(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return []

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "clean"},
        )
        assert result is None

    def test_empty_iterator_returns_none(self, work_dir: Path, session_dir: Path) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            return iter([])

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            trigger="x",
        )
        assert result is None


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


class TestTriggerIgnoredInStringMode:
    def test_trigger_absent_still_runs_string_check(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            return ["found"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content without trigger"},
            trigger="xyz",
        )
        assert result is not None
        assert "found" in result["hookSpecificOutput"]["additionalContext"]


class TestCheckRaises:
    def test_string_check_raises_returns_none(self, session_dir: Path) -> None:

        def check(content: str) -> list[str]:
            raise ValueError("boom")

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
        )
        assert result is None

    def test_ast_check_raises_returns_none(self, work_dir: Path, session_dir: Path) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            raise TypeError("oops")

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            trigger="x",
        )
        assert result is None


class TestDefaultEvents:
    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            pytest.param(None, Event.PostToolUse, id="default_is_post_tool_use"),
            pytest.param(Event.PreToolUse, Event.PreToolUse, id="can_be_overridden"),
        ],
    )
    def test_event(self, events: Event | None, expected: Event) -> None:

        def check(content: str) -> list[str]:
            return []

        register_lint(check, message="{violations}", events=events)
        assert _state.hooks[-1].spec.events == expected


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

        lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "class Foo"},
            trigger="class",
        )
        assert "class:Foo" in found_nodes
        assert "func:bar" in found_nodes


# Regression: lint() passes empty string through to checker (only skips None)


class TestEmptyStringPassthrough:
    def test_empty_string_content_reaches_checker(self, session_dir: Path) -> None:
        received_content: list[str | None] = []

        def check(content: str) -> list[str]:
            received_content.append(content)
            return ["empty file edit"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "old", "new_string": ""},
        )
        assert len(received_content) == 1
        assert received_content[0] == ""
        assert result is not None

    def test_none_content_still_skips(self, session_dir: Path) -> None:
        check_called = False

        def check(content: str) -> list[str]:
            nonlocal check_called
            check_called = True
            return ["should not fire"]

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_name="Bash",
            tool_input={"command": "echo hi"},
        )
        assert not check_called
        assert result is None


# Regression: lint() logs check function exceptions instead of silently swallowing


# Regression: AST lint passes empty .py file through ast.parse('') instead of
# skipping it. Only None (missing source) should be skipped.


class TestAstLintEmptyFile:
    def test_empty_py_file_runs_ast_check(self, work_dir: Path, session_dir: Path) -> None:
        py_file = work_dir / "empty.py"
        py_file.write_text("")
        received_tree: list[ast.AST] = []

        def check(tree: ast.AST) -> Iterator[str]:
            received_tree.append(tree)
            return iter([])

        lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": ""},
        )
        assert len(received_tree) == 1
        assert isinstance(received_tree[0], ast.Module)

    def test_empty_py_file_with_violations_fires(self, work_dir: Path, session_dir: Path) -> None:
        py_file = work_dir / "empty.py"
        py_file.write_text("")

        def check(tree: ast.AST) -> Iterator[str]:
            if not list(ast.walk(tree)):
                yield "empty module"
            yield "always fires"

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": ""},
        )
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
    def test_string_check_exception_is_logged(self, session_dir: Path, logcap: Any) -> None:
        def check(content: str) -> list[str]:
            raise ValueError("intentional boom")

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "content"},
        )
        assert result is None
        assert any("check" in r.message and r.exc_info for r in logcap.records)

    def test_ast_check_exception_is_logged(
        self,
        work_dir: Path,
        session_dir: Path,
        logcap: Any,
    ) -> None:
        source = "x = 1\n"
        py_file = work_dir / "code.py"
        py_file.write_text(source)

        def check(tree: ast.AST) -> Iterator[str]:
            raise TypeError("ast boom")

        result = lint_and_dispatch(
            session_dir,
            check,
            tool_input={"file_path": str(py_file), "old_string": "", "new_string": "x = 1"},
            trigger="x",
        )
        assert result is None
        assert any("check" in r.message and r.exc_info for r in logcap.records)


class TestPatternModeLint:
    def test_pattern_flags_match_with_line(self, session_dir: Path) -> None:
        from captain_hook.primitives.lint import lint

        lint(pattern="print($$$)", message="Found: {violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": 'x = 1\nprint("hi")\n'},
            ctx=make_ctx(session_dir),
        )
        result = dispatch(Event.PostToolUse, evt, session_dir)
        assert result is not None
        assert 'print("hi") (line 2)' in result["hookSpecificOutput"]["additionalContext"]

    def test_pattern_no_match_returns_none(self, session_dir: Path) -> None:
        from captain_hook.primitives.lint import lint

        lint(pattern="print($$$)", message="Found: {violations}")
        evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": "", "new_string": "x = 1\n"},
            ctx=make_ctx(session_dir),
        )
        assert dispatch(Event.PostToolUse, evt, session_dir) is None

    def test_lang_drives_file_guard(self, session_dir: Path) -> None:
        from captain_hook.primitives.lint import lint

        lint(pattern="console.log($$$)", message="No console.log: {violations}", lang="ts")
        ts_evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "app.ts", "old_string": "", "new_string": "console.log(x)\n"},
            ctx=make_ctx(session_dir),
        )
        assert dispatch(Event.PostToolUse, ts_evt, session_dir) is not None
        py_evt = make_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "app.py", "old_string": "", "new_string": "console.log(x)\n"},
            ctx=make_ctx(session_dir),
        )
        assert dispatch(Event.PostToolUse, py_evt, session_dir) is None

    def test_requires_exactly_one_of_check_or_pattern(self) -> None:
        from captain_hook.primitives.lint import lint

        with pytest.raises(TypeError, match="either a check function or pattern"):
            lint(message="m")
        with pytest.raises(TypeError, match="either a check function or pattern"):
            lint(lambda c: [], pattern="print($$$)", message="m")
