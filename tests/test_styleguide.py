from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from captain_hook.app import _state
from captain_hook.dispatch import dispatch
from captain_hook.style import (
    Change,
    StyleDiffRule,
    StyleRule,
    Violation,
    ast_grep_diff_rule,
    ast_grep_rule,
    styleguide,
)
from captain_hook.style import matchers as M
from captain_hook.types import Event, FilePath
from captain_hook.util import kebab
from tests.helpers import make_ctx, make_post_tool_event


class NoPrint(StyleRule):
    """
    print() calls don't belong in committed code:
      - {violations}

    Use a logger instead.
    """

    def check(self, change: Change) -> Iterator[Violation]:
        for node in ast.walk(change.tree):
            match node:
                case ast.Call(func=ast.Name(id="print")):
                    yield Violation(node.lineno, "print() call")


class NoLambda(StyleRule):
    """
    Named lambdas hurt readability:
      - {violations}
    """

    def check(self, change: Change) -> Iterator[Violation]:
        yield from (Violation(n.lineno, "lambda") for n in ast.walk(change.tree) if isinstance(n, ast.Lambda))


class NoNewGlobal(StyleDiffRule):
    """
    `global` statement newly introduced:
      - {violations}
    """

    def check(self, change: Change) -> Iterator[Violation]:
        old = {name for node in ast.walk(change.pre_tree) if isinstance(node, ast.Global) for name in node.names}
        for node in ast.walk(change.tree):
            if isinstance(node, ast.Global):
                yield from (Violation(node.lineno, f"global {name}") for name in node.names if name not in old)


class MissingDocstring(StyleRule):
    def check(self, change: Change) -> Iterator[Violation]:
        return iter(())


class UnoverriddenCheck(StyleRule):
    """Has a docstring but no check."""


def edit_event(session_dir: Path, *, file: str, old: str, new: str) -> object:
    return make_post_tool_event(
        tool_name="Edit",
        tool_input={"file_path": file, "old_string": old, "new_string": new},
        ctx=make_ctx(session_dir),
    )


def write_event(session_dir: Path, *, file: str, content: str) -> object:
    return make_post_tool_event(
        tool_name="Write",
        tool_input={"file_path": file, "content": content},
        ctx=make_ctx(session_dir),
    )


def warn_text(result: dict | None) -> str:
    assert result is not None
    return result["hookSpecificOutput"]["additionalContext"]


def run_edit(session_dir: Path, *, file: str, old: str, new: str) -> dict | None:
    return dispatch(Event.PostToolUse, edit_event(session_dir, file=file, old=old, new=new), session_dir)


def run_write(session_dir: Path, *, file: str, content: str) -> dict | None:
    return dispatch(Event.PostToolUse, write_event(session_dir, file=file, content=content), session_dir)


class TestBasics:
    def test_warns_with_line_number(self, session_dir: Path) -> None:
        styleguide(NoPrint)
        assert "print() call (line 1)" in warn_text(run_edit(session_dir, file="a.py", old="", new='print("hi")\n'))

    def test_clean_code_passes(self, session_dir: Path) -> None:
        styleguide(NoPrint)
        assert run_edit(session_dir, file="a.py", old="", new="x = 1\n") is None

    def test_one_hook_per_call(self, session_dir: Path) -> None:
        styleguide(NoPrint, NoLambda)
        assert len(_state.hooks) == 1

    def test_block_mode_denies(self, session_dir: Path) -> None:
        styleguide(NoPrint, block=True)
        result = run_edit(session_dir, file="a.py", old="", new='print("x")\n')
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestAggregation:
    def test_two_rules_aggregate_into_one_message(self, session_dir: Path) -> None:
        styleguide(NoPrint, NoLambda)
        msg = warn_text(run_edit(session_dir, file="a.py", old="", new="f = lambda: print(1)\n"))
        assert "print() call" in msg and "lambda" in msg
        assert "Use a logger instead." in msg and "hurt readability" in msg

    def test_only_firing_rules_appear(self, session_dir: Path) -> None:
        styleguide(NoPrint, NoLambda)
        msg = warn_text(run_edit(session_dir, file="a.py", old="", new="print(1)\n"))
        assert "print() call" in msg and "lambda" not in msg


class TestChangeScoping:
    SOURCE = 'def a():\n    print("a")\n\ndef b():\n    pass\n'
    EDITED = 'def a():\n    print("a")\n\ndef b():\n    print("b")\n'

    def test_preexisting_violation_in_untouched_region_suppressed(self, work_dir: Path, session_dir: Path) -> None:
        path = work_dir / "m.py"
        path.write_text(self.EDITED)
        styleguide(NoPrint)
        msg = warn_text(run_edit(session_dir, file=str(path), old="    pass", new='    print("b")'))
        assert "line 5" in msg
        assert "line 2" not in msg

    def test_write_reports_whole_file(self, work_dir: Path, session_dir: Path) -> None:
        path = work_dir / "m.py"
        path.write_text(self.EDITED)
        styleguide(NoPrint)
        msg = warn_text(run_write(session_dir, file=str(path), content=self.EDITED))
        assert "line 2" in msg and "line 5" in msg

    def test_partial_fragment_never_raises(self, work_dir: Path, session_dir: Path) -> None:
        path = work_dir / "m.py"
        path.write_text("def f():\n    if True:\n        print('x')\n    return 1\n")
        styleguide(NoPrint)
        assert run_edit(session_dir, file=str(path), old="        pass", new="        print('x')") is not None


class TestMaxShown:
    def test_max_shown_caps_violations(self, session_dir: Path) -> None:
        styleguide(NoPrint, max_shown=2)
        msg = warn_text(run_edit(session_dir, file="a.py", old="", new="print(1)\nprint(2)\nprint(3)\nprint(4)\n"))
        assert msg.count("(line ") == 2


class TestDiffRule:
    def test_newly_introduced_flagged(self, session_dir: Path) -> None:
        styleguide(NoNewGlobal)
        msg = warn_text(
            run_edit(
                session_dir, file="d.py", old="def f():\n    return 1\n", new="def f():\n    global x\n    return x\n"
            )
        )
        assert "global x" in msg

    def test_preexisting_not_flagged(self, session_dir: Path) -> None:
        styleguide(NoNewGlobal)
        result = run_edit(
            session_dir,
            file="d.py",
            old="def f():\n    global x\n    return 1\n",
            new="def f():\n    global x\n    return 2\n",
        )
        assert result is None

    def test_write_introduces_global(self, work_dir: Path, session_dir: Path) -> None:
        path = work_dir / "d.py"
        content = "def f():\n    global y\n    return y\n"
        path.write_text(content)
        styleguide(NoNewGlobal)
        assert "global y" in warn_text(run_write(session_dir, file=str(path), content=content))


class TestDocstringMessage:
    def test_cleandoc_strips_leading_newline(self, session_dir: Path) -> None:
        styleguide(NoPrint)
        msg = warn_text(run_edit(session_dir, file="a.py", old="", new="print(1)\n"))
        assert msg.startswith("print() calls don't belong")

    def test_appends_violations_when_no_placeholder(self, session_dir: Path) -> None:
        class NoCall(StyleRule):
            """
            Calls are not allowed in this file.
            """

            def check(self, change: Change) -> Iterator[Violation]:
                yield from (Violation(n.lineno, "call") for n in ast.walk(change.tree) if isinstance(n, ast.Call))

        styleguide(NoCall)
        msg = warn_text(run_edit(session_dir, file="a.py", old="", new="f()\n"))
        assert "Calls are not allowed in this file." in msg
        assert "call (line 1)" in msg

    def test_rule_tests_merge_into_hook(self, session_dir: Path) -> None:
        from captain_hook.testing import Allow, Input, Warn

        class A(StyleRule):
            """A: {violations}"""

            tests = {Input(file="a.py", content="print(1)\n"): Warn()}

            def check(self, change: Change) -> Iterator[Violation]:
                yield from (Violation(n.lineno, "p") for n in ast.walk(change.tree) if isinstance(n, ast.Call))

        class B(StyleRule):
            """B: {violations}"""

            tests = {Input(file="a.py", content="x = 1\n"): Allow()}

            def check(self, change: Change) -> Iterator[Violation]:
                return iter(())

        styleguide(A, B)
        assert len(_state.hooks[-1].spec.tests) == 2


class TestValidation:
    @pytest.mark.parametrize(
        ("rule", "exc_type", "match"),
        [
            pytest.param(MissingDocstring, ValueError, "docstring", id="rejects_missing_docstring"),
            pytest.param(UnoverriddenCheck, TypeError, "check", id="rejects_unoverridden_check"),
            pytest.param(int, TypeError, "StyleRule", id="rejects_non_rule"),
        ],
    )
    def test_rejects(self, rule: type, exc_type: type[Exception], match: str) -> None:
        with pytest.raises(exc_type, match=match):
            styleguide(rule)  # type: ignore[arg-type]


class TestScoping:
    def test_appends_only_if_guard(self, session_dir: Path) -> None:
        styleguide(NoPrint, only_if=[FilePath("src/**/*.py")])
        assert run_edit(session_dir, file="other/a.py", old="", new="print(1)\n") is None

    def test_default_event_is_post_tool_use(self) -> None:
        styleguide(NoPrint)
        assert _state.hooks[-1].spec.events == Event.PostToolUse


class TestHelpers:
    def test_kebab(self) -> None:
        assert kebab("NoNestedImports") == "no-nested-imports"
        assert kebab("ZipStrict") == "zip-strict"


class TestMatcher:
    @pytest.mark.parametrize(
        ("source", "matcher", "count"),
        [
            pytest.param(
                "def f(c):\n    if c:\n        import os\n",
                M.imports & M.child_of(M.control_flow),
                1,
                id="child_of",
            ),
            pytest.param(
                "pairs = list(zip(a, b))\nok = zip(a, b, strict=True)\n",
                M.calls("zip") & ~M.kwarg("strict"),
                1,
                id="intersection_and_negation",
            ),
        ],
    )
    def test_match_count(self, source: str, matcher: M.Matcher, count: int) -> None:
        assert len(list(matcher.over(ast.parse(source)))) == count

    def test_negated_under_type_checking(self) -> None:
        tree = ast.parse("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import os\n")
        match = M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)
        assert list(match.over(tree)) == []

    def test_following_boundary(self) -> None:
        late = ast.parse("def f():\n    pass\nMAX = 3\n")
        early = ast.parse("MAX = 3\ndef f():\n    pass\n")
        match = M.assignment & M.child_of(M.module) & M.following(M.definition)
        assert len(list(match.over(late))) == 1
        assert list(match.over(early)) == []

    def test_union(self) -> None:
        tree = ast.parse("class C:\n    pass\ndef f():\n    pass\n")
        assert sum((M.cls | M.func).matches(n) for n in ast.walk(tree)) == 2

    def test_named_presets(self) -> None:
        tree = ast.parse("class _P:\n    pass\nMAX_RETRIES = 3\n")
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        const = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign))
        assert M.private.matches(cls)
        assert M.constant.matches(const)
        assert not M.dunder.matches(cls)

    def test_violations_label(self) -> None:
        tree = ast.parse("import os\n")
        assert list(M.imports.violations(tree, "import")) == [Violation(1, "import")]

    def test_matches_raises_on_structural(self) -> None:
        with pytest.raises(ValueError, match="structural"):
            M.under(M.module).matches(ast.parse("x = 1\n"))


class TestAstGrepRules:
    def test_string_pattern_warns_with_line(self, session_dir: Path) -> None:
        rule = ast_grep_rule("NoPrintAg", pattern="print($$$)", message="No print: {violations}", label="print() call")
        styleguide(rule)
        evt = edit_event(session_dir, file="a.py", old="", new='x = 1\nprint("hi")\n')
        assert "print() call (line 2)" in warn_text(dispatch(Event.PostToolUse, evt, session_dir))

    def test_clean_code_passes(self, session_dir: Path) -> None:
        rule = ast_grep_rule("NoEvalAg", pattern="eval($$$)", message="No eval: {violations}", label="eval")
        styleguide(rule)
        evt = edit_event(session_dir, file="a.py", old="", new="x = 1\n")
        assert dispatch(Event.PostToolUse, evt, session_dir) is None

    def test_default_label_is_matched_code(self, session_dir: Path) -> None:
        rule = ast_grep_rule("NoEvalAg", pattern="eval($$$)", message="No eval: {violations}")
        styleguide(rule)
        msg = warn_text(
            dispatch(Event.PostToolUse, edit_event(session_dir, file="a.py", old="", new="eval('x')\n"), session_dir)
        )
        assert "eval('x') (line 1)" in msg

    def test_diff_rule_flags_new(self, session_dir: Path) -> None:
        rule = ast_grep_diff_rule("NoNewWildcardAg", pattern="from $MOD import *", message="New wildcard: {violations}")
        styleguide(rule)
        evt = edit_event(session_dir, file="d.py", old="import os\n", new="from os import *\n")
        assert "from os import *" in warn_text(dispatch(Event.PostToolUse, evt, session_dir))

    def test_diff_rule_preexisting_not_flagged(self, session_dir: Path) -> None:
        rule = ast_grep_diff_rule("NoNewWildcardAg", pattern="from $MOD import *", message="New wildcard: {violations}")
        styleguide(rule)
        evt = edit_event(session_dir, file="d.py", old="from os import *\n", new="from os import *\nx = 1\n")
        assert dispatch(Event.PostToolUse, evt, session_dir) is None

    def test_change_scoping_suppresses_untouched_match(self, work_dir: Path, session_dir: Path) -> None:
        path = work_dir / "m.py"
        path.write_text('def a():\n    print("a")\n\ndef b():\n    print("b")\n')
        rule = ast_grep_rule("NoPrintAg", pattern="print($$$)", message="No print: {violations}", label="print")
        styleguide(rule)
        evt = edit_event(session_dir, file=str(path), old="    pass", new='    print("b")')
        msg = warn_text(dispatch(Event.PostToolUse, evt, session_dir))
        assert "line 5" in msg and "line 2" not in msg

    def test_mixes_with_python_rule_in_one_call(self, session_dir: Path) -> None:
        ag = ast_grep_rule("NoPrintAg", pattern="print($$$)", message="No print: {violations}", label="print() call")
        styleguide(ag, NoLambda)
        evt = edit_event(session_dir, file="a.py", old="", new="f = lambda: print(1)\n")
        msg = warn_text(dispatch(Event.PostToolUse, evt, session_dir))
        assert "print() call" in msg and "lambda" in msg


class TestDeclarative:
    def test_match_only_rule_warns(self, session_dir: Path) -> None:
        class NoBareZip(StyleRule):
            """No bare zip(): {violations}"""

            match = M.calls("zip") & ~M.kwarg("strict")
            label = "zip()"

        styleguide(NoBareZip)
        msg = warn_text(
            dispatch(Event.PostToolUse, edit_event(session_dir, file="a.py", old="", new="zip(a, b)\n"), session_dir)
        )
        assert "zip() (line 1)" in msg

    def test_match_only_rule_passes_clean(self, session_dir: Path) -> None:
        class NoBareZip(StyleRule):
            """No bare zip(): {violations}"""

            match = M.calls("zip") & ~M.kwarg("strict")

        styleguide(NoBareZip)
        evt = edit_event(session_dir, file="a.py", old="", new="zip(a, b, strict=True)\n")
        assert dispatch(Event.PostToolUse, evt, session_dir) is None

    def test_diff_match_rule_flags_new(self, session_dir: Path) -> None:
        class NoNewAny(StyleDiffRule):
            """New Any annotation: {violations}"""

            match = M.annotated(M.ref("Any"))

        styleguide(NoNewAny)
        evt = edit_event(session_dir, file="d.py", old="x: int\n", new="x: Any\n")
        assert "x (line 1)" in warn_text(dispatch(Event.PostToolUse, evt, session_dir))
