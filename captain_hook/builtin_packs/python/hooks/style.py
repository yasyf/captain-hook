from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from captain_hook import Allow, Input, Warn
from captain_hook.style import Change, StyleDiffRule, StyleRule, Violation, styleguide
from captain_hook.style import matchers as M


def any_label(node: ast.AST) -> str:
    match node:
        case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
            return f"{name}() -> Any"
        case ast.AnnAssign(target=ast.Name(id=name)) | ast.arg(arg=name):
            return f"{name}: Any"
        case _:
            return "Any"


class NoUnderscorePrefixes(StyleRule):
    """
    Underscore-prefixed class, constant, or module name: {violations}
    Rename it without the leading underscore.
    """

    tests = {
        Input(file="m.py", content="class _Helper:\n    pass\n"): Warn(),
        Input(file="m.py", content="_MAX_RETRIES = 3\n"): Warn(),
        Input(file="m.py", content="class Helper:\n    pass\n"): Allow(),
        Input(file="m.py", content="MAX_RETRIES = 3\n"): Allow(),
        Input(file="_common.py", content="value = 1\n"): Warn(),
        Input(file="pkg/_common.py", content="value = 1\n"): Warn(),
        Input(file="common.py", content="value = 1\n"): Allow(),
        Input(file="__init__.py", content="value = 1\n"): Allow(),
    }
    match = M.private & (M.cls | (M.assignment & M.constant))

    def check(self, change: Change) -> Iterator[Violation]:
        yield from super().check(change)
        if Path(change.path).match("_[!_]*"):
            yield Violation(line=1, label=f"module filename {Path(change.path).name!r}")


class NoNestedImports(StyleRule):
    """
    Import nested inside control flow: {violations}
    Move it to the top of the function body.
    """

    tests = {
        Input(file="m.py", content="def f(cond):\n    if cond:\n        import os\n    return os\n"): Warn(),
        Input(file="m.py", content="def f(cond):\n    import os\n\n    return os if cond else None\n"): Allow(),
        Input(file="m.py", content="from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import os\n"): Allow(),
    }
    match = M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)


class ZipStrict(StyleRule):
    """
    `zip()` call without `strict=True`: {violations}
    Add `strict=True`.
    """

    tests = {
        Input(file="m.py", content="pairs = list(zip(a, b))\n"): Warn(),
        Input(file="m.py", content="pairs = list(zip(a, b, strict=True))\n"): Allow(),
    }
    label = "zip()"
    match = M.calls("zip") & ~M.kwarg("strict")


class LateModuleConstants(StyleRule):
    """
    Module constant placed after a class or function: {violations}
    Move it directly below the imports.
    """

    tests = {
        Input(file="m.py", content="def f():\n    pass\n\n\nMAX = 3\n"): Warn(),
        Input(file="m.py", content="MAX = 3\n\n\ndef f():\n    pass\n"): Allow(),
    }
    match = M.assignment & M.child_of(M.module) & M.following(M.definition) & M.constant


class LateClassConstants(StyleRule):
    """
    Class assignment placed after a method: {violations}
    Move it above the first method.
    """

    tests = {
        Input(file="m.py", content="class C:\n    def m(self):\n        pass\n\n    X = 3\n"): Warn(),
        Input(file="m.py", content="class C:\n    X = 3\n\n    def m(self):\n        pass\n"): Allow(),
    }
    match = M.assignment & M.child_of(M.cls) & M.following(M.func)


class NoQuotedAnnotations(StyleRule):
    """
    Quoted annotation in a file using `from __future__ import annotations`: {violations}
    Drop the quotes.
    """

    tests = {
        Input(file="m.py", content='from __future__ import annotations\n\nx: "Foo" = None\n'): Warn(),
        Input(file="m.py", content="from __future__ import annotations\n\nx: Foo = None\n"): Allow(),
    }
    match = M.forward_ref & M.under(M.future_annotations)


class NoWeakeningToAny(StyleDiffRule):
    """
    Typed slot widened to `Any`: {violations}
    Use the real type instead.
    """

    tests = {
        Input(file="x.py", old="def foo() -> Result:\n    ...", content="def foo() -> Any:\n    ..."): Warn(),
        Input(file="x.py", old="x: list[Foo]", content="x: Any"): Warn(),
        Input(file="x.py", old="def f(x: int):\n    ...", content="def f(x: Any):\n    ..."): Warn(),
        Input(file="x.py", old="x: Any", content="x: Any"): Allow(),
        Input(file="x.py", old="", content="def f(*args: Any, **kwargs: Any) -> None:\n    ..."): Allow(),
        Input(file="x.py", old="", content="JsonDict = dict[str, Any]"): Allow(),
        Input(
            file="x.py", old="def f() -> dict[str, Foo]:\n    ...", content="def f() -> dict[str, Any]:\n    ..."
        ): Allow(),
    }

    def check(self, change: Change) -> Iterator[Violation]:
        yield from M.annotated(M.ref("Any")).diff(change.pre_tree, change.tree, key=any_label, label=any_label)


styleguide(
    NoUnderscorePrefixes,
    NoNestedImports,
    ZipStrict,
    LateModuleConstants,
    LateClassConstants,
    NoQuotedAnnotations,
    NoWeakeningToAny,
)
