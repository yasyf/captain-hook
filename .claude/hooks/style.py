from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from captain_hook import Allow, Input, StyleDiffRule, StyleRule, Violation, Warn, gate, styleguide
from captain_hook.styleguide import (
    Assignment,
    Class,
    ControlFlow,
    Definition,
    Function,
    Import,
    Module,
    Query,
    Slot,
    TypeChecking,
    annotated_slots,
    annotations,
    calls,
    has_future_annotations,
    has_keyword,
    is_name,
    name_of,
    string_literals,
)

CONST_UNDERSCORE = re.compile(r"^_[A-Z][A-Z_0-9]*$")
UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def underscored(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def any_label(slot: Slot) -> str:
    return f"{slot.name}() -> Any" if slot.is_return else f"{slot.name}: Any"


class NoUnderscorePrefixes(StyleRule):
    """
    This edit introduces underscore-prefixed class(es) or constant(s).

    captain-hook never uses leading underscores on classes, constants, or module-level
    helpers — use `__all__` for export control instead. See STYLEGUIDE.md § Code Organization.
    """

    tests = {
        Input(file="m.py", content="class _Helper:\n    pass\n"): Warn(),
        Input(file="m.py", content="_MAX_RETRIES = 3\n"): Warn(),
        Input(file="m.py", content="class Helper:\n    pass\n"): Allow(),
        Input(file="m.py", content="__all__ = ['Helper']\n"): Allow(),
    }

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        q = Query.of(tree)
        yield from q.matching(Class).where(lambda n: underscored(n.name)).violations(lambda n: f"class {n.name}")
        yield from q.matching(Assignment).where(lambda n: bool(CONST_UNDERSCORE.match(name_of(n) or ""))).violations(lambda n: name_of(n) or "")


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

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        yield from Query.of(tree).matching(Import).directly_inside(ControlFlow).not_inside(TypeChecking).violations(ast.unparse)


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

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        yield from Query.of(tree).matching(calls("zip")).where(lambda c: not has_keyword(c, "strict")).violations("zip()")


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

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        yield from (
            Query.of(tree)
            .matching(Assignment)
            .directly_inside(Module)
            .after_first(Definition)
            .where(lambda n: bool(UPPER_SNAKE.match(name_of(n) or "")))
            .violations(lambda n: name_of(n) or "")
        )


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

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        yield from (
            Query.of(tree).matching(Assignment).directly_inside(Class).after_first(Function).violations(lambda n: name_of(n) or "")
        )


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

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        if not has_future_annotations(tree):
            return
        yield from (
            Violation(const.lineno, repr(const.value)) for ann in annotations(tree) for const in string_literals(ann)
        )


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
        Input(file="x.py", old="def f() -> dict[str, Foo]:\n    ...", content="def f() -> dict[str, Any]:\n    ..."): Allow(),
    }

    def check(self, pre: ast.Module, post: ast.Module) -> Iterator[Violation]:
        old = {any_label(s) for s in annotated_slots(pre) if is_name(s.annotation, "Any")}
        yield from (
            Violation(s.line, any_label(s)) for s in annotated_slots(post) if is_name(s.annotation, "Any") and any_label(s) not in old
        )


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
