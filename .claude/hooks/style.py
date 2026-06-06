from __future__ import annotations

import ast

from captain_hook import Allow, Input, StyleDiffRule, StyleRule, Warn, gate, styleguide
from captain_hook.styleguide import Matcher as M


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
    This edit introduces underscore-prefixed class(es) or constant(s): {violations}

    captain-hook never uses leading underscores on classes, constants, or module-level
    helpers — use `__all__` for export control instead. See STYLEGUIDE.md § Code Organization.
    """

    tests = {
        Input(file="m.py", content="class _Helper:\n    pass\n"): Warn(),
        Input(file="m.py", content="_MAX_RETRIES = 3\n"): Warn(),
        Input(file="m.py", content="class Helper:\n    pass\n"): Allow(),
        Input(file="m.py", content="__all__ = ['Helper']\n"): Allow(),
    }
    match = M.private & (M.cls | (M.assignment & M.constant))


class NoNestedImports(StyleRule):
    """
    This edit nests import(s) inside control flow.

    Lazy imports go at the TOP of the function body, before any logic — never inside
    if/for/try/with blocks. Move them up. See STYLEGUIDE.md § Type Annotations.
    """

    tests = {
        Input(file="m.py", content="def f(cond):\n    if cond:\n        import os\n    return os\n"): Warn(),
        Input(file="m.py", content="def f(cond):\n    import os\n\n    return os if cond else None\n"): Allow(),
        Input(file="m.py", content="from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import os\n"): Allow(),
    }
    match = M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)


class ZipStrict(StyleRule):
    """
    This edit uses zip() without strict=True.

    Always use zip(..., strict=True) to catch length mismatches early.
    """

    trigger = "zip"
    tests = {
        Input(file="m.py", content="pairs = list(zip(a, b))\n"): Warn(),
        Input(file="m.py", content="pairs = list(zip(a, b, strict=True))\n"): Allow(),
    }
    match = M.calls("zip") & ~M.kwarg("strict")
    label = "zip()"


class LateModuleConstants(StyleRule):
    """
    This edit places module constant(s) after a class or function.

    Module-level UPPER_SNAKE_CASE constants belong immediately after imports, before any
    class or function. Move them up. See STYLEGUIDE.md § Code Organization.
    """

    tests = {
        Input(file="m.py", content="def f():\n    pass\n\n\nMAX = 3\n"): Warn(),
        Input(file="m.py", content="MAX = 3\n\n\ndef f():\n    pass\n"): Allow(),
    }
    match = M.assignment & M.child_of(M.module) & M.following(M.definition) & M.constant


class LateClassConstants(StyleRule):
    """
    This edit places class assignment(s) after a method.

    Within a class body, all assignments (constants, ClassVars, dataclass fields) come
    before any method. Move them up. See STYLEGUIDE.md § Code Organization.
    """

    tests = {
        Input(file="m.py", content="class C:\n    def m(self):\n        pass\n\n    X = 3\n"): Warn(),
        Input(file="m.py", content="class C:\n    X = 3\n\n    def m(self):\n        pass\n"): Allow(),
    }
    match = M.assignment & M.child_of(M.cls) & M.following(M.func)


class NoQuotedAnnotations(StyleRule):
    """
    This edit has quoted annotation(s) in a file using `from __future__ import annotations`.

    Under PEP 563 every annotation is already deferred, so the quotes are redundant — drop
    them (e.g. `x: "MyType"` → `x: MyType`). See STYLEGUIDE.md § Type Annotations.
    """

    trigger = "from __future__ import annotations"
    tests = {
        Input(file="m.py", content='from __future__ import annotations\n\nx: "Foo" = None\n'): Warn(),
        Input(file="m.py", content="from __future__ import annotations\n\nx: Foo = None\n"): Allow(),
    }
    match = M.forward_ref & M.under(M.future_annotations)


class NoWeakeningToAny(StyleDiffRule):
    """
    This edit introduces `Any` annotation(s).

    Don't widen a typed slot to `Any` to silence the type checker. Use the real type
    (check imports/usage), narrow with isinstance, or split the model.
    See STYLEGUIDE.md § Type Annotations.
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
    match = M.annotated(M.ref("Any"))
    key = any_label
    label = any_label


gate(
    "You wrote new Python code but haven't done a style review. Before stopping, "
    "review your changes against STYLEGUIDE.md (functional over imperative, no underscore "
    "prefixes, match for type dispatch, minimal try/except, make invalid states "
    "unrepresentable, flat over nested). Fix any violations in the code you wrote.",
    when=lambda evt: any(
        f.matches("**/captain_hook/**/*.py") and not f.is_test for f in evt.ctx.t.extract_files(["Edit", "Write"])
    ),
)

styleguide(
    NoUnderscorePrefixes,
    NoNestedImports,
    ZipStrict,
    LateModuleConstants,
    LateClassConstants,
    NoQuotedAnnotations,
    NoWeakeningToAny,
)
