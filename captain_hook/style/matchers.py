"""Composable AST matchers for style rules — import this module as ``M``.

Every factory (``kind``, ``calls``, ``under``, ...) and preset (``imports``,
``control_flow``, ...) is a module-level [`Matcher`][captain_hook.style.matchers.Matcher]
builder or constant; author a rule by combining them with ``&``, ``|``, and ``~``.

Example:
    >>> from captain_hook.style import matchers as M
    >>> M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Hashable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from captain_hook.style.types import Violation

Parents = Mapping[ast.AST, ast.AST]
Predicate = Callable[[ast.AST, Parents], bool]


@runtime_checkable
class Positioned(Protocol):
    lineno: int


CONTROL_FLOW = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
ASSIGNMENTS = (ast.Assign, ast.AnnAssign)


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def name_of(node: ast.AST) -> str | None:
    match node:
        case ast.ClassDef(name=name) | ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
            return name
        case ast.Assign(targets=[ast.Name(id=name), *_]) | ast.AnnAssign(target=ast.Name(id=name)):
            return name
        case ast.arg(arg=name):
            return name
        case _:
            return None


def describe(node: ast.AST) -> str:
    return name_of(node) or ast.unparse(node)


def line_of(node: ast.AST) -> int:
    return node.lineno if isinstance(node, Positioned) else 1


def body_of(node: ast.AST | None) -> list[ast.stmt] | None:
    match node:
        case (
            ast.Module(body=body)
            | ast.FunctionDef(body=body)
            | ast.AsyncFunctionDef(body=body)
            | ast.ClassDef(body=body)
            | ast.For(body=body)
            | ast.AsyncFor(body=body)
            | ast.While(body=body)
            | ast.If(body=body)
            | ast.With(body=body)
            | ast.AsyncWith(body=body)
            | ast.Try(body=body)
        ):
            return body
        case _:
            return None


def label_for(label: str | Callable[[ast.AST], str] | None, node: ast.AST) -> str:
    match label:
        case None:
            return describe(node)
        case str():
            return label
        case _:
            return label(node)


def annotation_of(node: ast.AST) -> ast.expr | None:
    match node:
        case ast.AnnAssign(annotation=ann):
            return ann
        case ast.FunctionDef(returns=ann) | ast.AsyncFunctionDef(returns=ann):
            return ann
        case ast.arg(annotation=ann):
            return ann
        case _:
            return None


def annotation_root(node: ast.AST, parents: Parents) -> ast.expr | None:
    cur: ast.AST = node
    while (p := parents.get(cur)) is not None:
        if annotation_of(p) is cur and isinstance(cur, ast.expr):
            return cur
        cur = p
    return None


def string_refs(node: ast.expr) -> Iterator[ast.Constant]:
    match node:
        case ast.Constant(value=str()):
            yield node
        case ast.Subscript(value=ast.Name(id="Literal") | ast.Attribute(attr="Literal")):
            return
        case ast.Subscript(
            value=ast.Name(id="Annotated") | ast.Attribute(attr="Annotated"), slice=ast.Tuple(elts=[first, *_])
        ):
            yield from string_refs(first)
        case _:
            yield from (
                ref for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr) for ref in string_refs(child)
            )


def is_type_checking(node: ast.AST) -> bool:
    match node:
        case ast.If(test=ast.Name(id="TYPE_CHECKING")):
            return True
        case ast.If(test=ast.Attribute(value=ast.Name(id="typing"), attr="TYPE_CHECKING")):
            return True
        case _:
            return False


def has_future_annotations(node: ast.AST) -> bool:
    return isinstance(node, ast.Module) and any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(alias.name == "annotations" for alias in stmt.names)
        for stmt in node.body
    )


def is_vararg(node: ast.arg, parents: Parents) -> bool:
    return isinstance(p := parents.get(node), ast.arguments) and node in (p.vararg, p.kwarg)


@dataclass(frozen=True, slots=True)
class Matcher:
    """A composable, immutable AST matcher: a node predicate that is also a tree selector.

    A `Matcher` wraps a single ``(node, parents) -> bool`` test. *Node-local* matchers
    (e.g. [`calls`][captain_hook.style.matchers.calls]) ignore ``parents``; *structural*
    matchers (e.g. [`under`][captain_hook.style.matchers.under]) consult it. Compose with
    the boolean algebra ``&`` (intersection), ``|`` (union), and ``~`` (negation), refine with
    [`where`][captain_hook.style.matchers.Matcher.where], and finish with a terminal —
    [`over`][captain_hook.style.matchers.Matcher.over],
    [`violations`][captain_hook.style.matchers.Matcher.violations], or
    [`exists`][captain_hook.style.matchers.Matcher.exists].

    The module-level vocabulary (``M.imports``, ``M.control_flow``, ...) and factories
    (``M.kind``, ``M.calls``, ...) are the whole surface — author a new rule by combining
    them, not by reaching for a framework helper.

    Example:
        >>> M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)
    """

    test: Predicate
    label: str = "matcher"
    structural: bool = False

    def __and__(self, other: Matcher) -> Matcher:
        return Matcher(
            lambda node, parents: self.test(node, parents) and other.test(node, parents),
            f"({self.label} & {other.label})",
            self.structural or other.structural,
        )

    def __or__(self, other: Matcher) -> Matcher:
        return Matcher(
            lambda node, parents: self.test(node, parents) or other.test(node, parents),
            f"({self.label} | {other.label})",
            self.structural or other.structural,
        )

    def __invert__(self) -> Matcher:
        return Matcher(lambda node, parents: not self.test(node, parents), f"~{self.label}", self.structural)

    def where(self, predicate: Callable[[ast.AST], bool]) -> Matcher:
        """Refine with a node-local one-off ``predicate`` — the escape hatch for bespoke conditions."""
        return Matcher(
            lambda node, parents: self.test(node, parents) and predicate(node),
            f"{self.label}.where(...)",
            self.structural,
        )

    def over(self, tree: ast.AST) -> Iterator[ast.AST]:
        """Yield every node in ``tree`` that matches."""
        parents = parent_map(tree)
        return (node for node in ast.walk(tree) if self.test(node, parents))

    def violations(self, tree: ast.AST, label: str | Callable[[ast.AST], str] | None = None) -> Iterator[Violation]:
        """Yield a [`Violation`][captain_hook.style.Violation] for each match, located at its line."""
        return (Violation(line_of(node), label_for(label, node)) for node in self.over(tree))

    def diff(
        self,
        pre: ast.AST,
        post: ast.AST,
        key: Callable[[ast.AST], Hashable] = ast.unparse,
        label: str | Callable[[ast.AST], str] | None = None,
    ) -> Iterator[Violation]:
        """Yield violations for matches in ``post`` whose ``key`` was absent from ``pre``."""
        before = {key(node) for node in self.over(pre)}
        return (Violation(line_of(node), label_for(label, node)) for node in self.over(post) if key(node) not in before)

    def exists(self, tree: ast.AST) -> bool:
        """Return whether any node in ``tree`` matches."""
        return any(True for _ in self.over(tree))

    def matches(self, node: ast.AST) -> bool:
        """Test a single ``node`` (node-local matchers only; raises for structural matchers)."""
        if self.structural:
            raise ValueError(
                f"matches() needs tree context for structural matcher {self.label!r}; use over()/violations()"
            )
        return self.test(node, {})


def kind(*types: type[ast.AST], label: str | None = None) -> Matcher:
    """Match any of the given AST node ``types`` — the primitive for a category not shipped."""
    return Matcher(lambda node, _parents: isinstance(node, types), label or "|".join(t.__name__ for t in types))


def calls(name: str) -> Matcher:
    """Match a call to the bare-name function ``name`` (e.g. ``calls("zip")``)."""
    return Matcher(
        lambda node, _parents: isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name,
        f"calls({name})",
    )


def kwarg(name: str) -> Matcher:
    """Match a call that passes the keyword argument ``name`` (combine with ``calls``)."""
    return Matcher(
        lambda node, _parents: isinstance(node, ast.Call) and any(kw.arg == name for kw in node.keywords),
        f"kwarg({name})",
    )


def ref(name: str) -> Matcher:
    """Match the bare name reference ``name`` (e.g. ``ref("Any")`` for ``x: Any``)."""
    return Matcher(lambda node, _parents: isinstance(node, ast.Name) and node.id == name, f"ref({name})")


def named(pattern: str | re.Pattern[str]) -> Matcher:
    """Match a class/function/assignment/parameter whose bound name matches ``pattern`` (``re.search``)."""
    rx = re.compile(pattern) if isinstance(pattern, str) else pattern
    return Matcher(
        lambda node, _parents: (nm := name_of(node)) is not None and rx.search(nm) is not None,
        f"named({rx.pattern})",
    )


def annotated(inner: Matcher | None = None) -> Matcher:
    """Match an annotation owner (annotated variable, parameter, or function return).

    Excludes ``*args``/``**kwargs``. When ``inner`` is given, the owner's annotation
    expression must also match it (e.g. ``annotated(ref("Any"))``).
    """

    def test(node: ast.AST, parents: Parents) -> bool:
        if (ann := annotation_of(node)) is None:
            return False
        if isinstance(node, ast.arg) and is_vararg(node, parents):
            return False
        return inner is None or inner.test(ann, parents)

    return Matcher(test, f"annotated({inner.label})" if inner else "annotated", structural=True)


def under(other: Matcher) -> Matcher:
    """Match a node with any ancestor matching ``other`` (negate with ``~`` for "not inside")."""

    def test(node: ast.AST, parents: Parents) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if other.test(cur, parents):
                return True
            cur = parents.get(cur)
        return False

    return Matcher(test, f"under({other.label})", structural=True)


def child_of(other: Matcher) -> Matcher:
    """Match a node whose immediate parent matches ``other``."""
    return Matcher(
        lambda node, parents: (p := parents.get(node)) is not None and other.test(p, parents),
        f"child_of({other.label})",
        structural=True,
    )


def following(boundary: Matcher) -> Matcher:
    """Match a body statement that follows the first sibling matching ``boundary``."""

    def test(node: ast.AST, parents: Parents) -> bool:
        if (body := body_of(parents.get(node))) is None:
            return False
        cut = next((i for i, stmt in enumerate(body) if boundary.test(stmt, parents)), None)
        here = next((i for i, stmt in enumerate(body) if stmt is node), None)
        return cut is not None and here is not None and here > cut

    return Matcher(test, f"following({boundary.label})", structural=True)


module: Matcher = kind(ast.Module, label="module")
cls: Matcher = kind(ast.ClassDef, label="class")
func: Matcher = kind(*FUNCTIONS, label="function")
definition: Matcher = cls | func
imports: Matcher = kind(ast.Import, ast.ImportFrom, label="import")
call: Matcher = kind(ast.Call, label="call")
assignment: Matcher = kind(*ASSIGNMENTS, label="assignment")
control_flow: Matcher = kind(*CONTROL_FLOW, label="control-flow")
type_checking: Matcher = Matcher(lambda node, _parents: is_type_checking(node), "type-checking")
future_annotations: Matcher = Matcher(lambda node, _parents: has_future_annotations(node), "future-annotations")
forward_ref: Matcher = Matcher(
    lambda node, parents: (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (root := annotation_root(node, parents)) is not None
        and node in set(string_refs(root))
    ),
    "forward-ref",
    structural=True,
)
private: Matcher = named(r"^_(?!_)")
dunder: Matcher = named(r"^__\w+__$")
constant: Matcher = named(r"^_?[A-Z][A-Z0-9_]*$")
