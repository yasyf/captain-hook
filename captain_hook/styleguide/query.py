from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace

from captain_hook.styleguide.types import Violation


@dataclass(frozen=True, slots=True)
class Kind:
    """A matchable category of AST node — a set of node types and/or a predicate.

    Compose with ``|`` (matches either), narrow with the constructor, or build one with the
    [`calls`][captain_hook.styleguide.calls] / [`named`][captain_hook.styleguide.named]
    factories. Authoring a new rule is usually a matter of combining existing `Kind`s rather
    than adding a framework helper.

    Example:
        >>> Definition = Function | Class
        >>> Decorated = Kind(test=lambda n: bool(getattr(n, "decorator_list", None)))
    """

    types: tuple[type[ast.AST], ...] = ()
    test: Callable[[ast.AST], bool] | None = None
    label: str = "Kind"

    def matches(self, node: ast.AST) -> bool:
        return (not self.types or isinstance(node, self.types)) and (self.test is None or self.test(node))

    def __or__(self, other: Kind) -> Kind:
        return Kind(test=lambda n: self.matches(n) or other.matches(n), label=f"{self.label}|{other.label}")


def is_type_checking_test(test: ast.expr) -> bool:
    """Return whether ``test`` is the ``TYPE_CHECKING`` / ``typing.TYPE_CHECKING`` condition."""
    match test:
        case ast.Name(id="TYPE_CHECKING"):
            return True
        case ast.Attribute(value=ast.Name(id="typing"), attr="TYPE_CHECKING"):
            return True
        case _:
            return False


Module = Kind((ast.Module,), label="Module")
Class = Kind((ast.ClassDef,), label="Class")
Function = Kind((ast.FunctionDef, ast.AsyncFunctionDef), label="Function")
Definition = Class | Function
Import = Kind((ast.Import, ast.ImportFrom), label="Import")
Call = Kind((ast.Call,), label="Call")
Assignment = Kind((ast.Assign, ast.AnnAssign), label="Assignment")
ControlFlow = Kind(
    (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler),
    label="ControlFlow",
)
TypeChecking = Kind((ast.If,), lambda n: is_type_checking_test(n.test), label="TypeChecking")


def calls(name: str) -> Kind:
    """A `Kind` matching a call to the named function, e.g. ``calls("zip")``."""
    return Kind((ast.Call,), lambda n: isinstance(n.func, ast.Name) and n.func.id == name, label=f"calls({name})")


def named(*names: str) -> Kind:
    """A `Kind` matching a class/function/assignment/argument bound to one of ``names``."""
    return Kind(test=lambda n: name_of(n) in names, label=f"named({', '.join(names)})")


@dataclass(frozen=True, slots=True)
class Query:
    """A fluent, immutable filter over the nodes of an AST tree.

    Start with ``Query.of(tree)``, chain refinements (each returns a new `Query`), and finish
    with a terminal — iterate it, call ``exists()``, or ``violations(label)`` to turn the
    survivors into [`Violation`][captain_hook.styleguide.Violation]s.

    Example:
        >>> Query.of(tree).matching(Import).directly_inside(ControlFlow).not_inside(TypeChecking)
    """

    tree: ast.Module
    items: tuple[ast.AST, ...]
    parents: Mapping[ast.AST, ast.AST]

    @classmethod
    def of(cls, tree: ast.Module) -> Query:
        return cls(tree, tuple(ast.walk(tree)), parent_map(tree))

    def matching(self, kind: Kind) -> Query:
        """Keep only nodes matching ``kind``."""
        return self.keeping(kind.matches)

    def where(self, predicate: Callable[[ast.AST], bool]) -> Query:
        """Keep only nodes satisfying ``predicate`` — the escape hatch for one-off conditions."""
        return self.keeping(predicate)

    def inside(self, kind: Kind) -> Query:
        """Keep nodes with any ancestor matching ``kind``."""
        return self.keeping(lambda n: any(kind.matches(a) for a in self.ancestors(n)))

    def directly_inside(self, kind: Kind) -> Query:
        """Keep nodes whose immediate parent matches ``kind``."""
        return self.keeping(lambda n: (p := self.parents.get(n)) is not None and kind.matches(p))

    def not_inside(self, kind: Kind) -> Query:
        """Keep nodes with no ancestor matching ``kind`` (e.g. ``not_inside(TypeChecking)``)."""
        return self.keeping(lambda n: not any(kind.matches(a) for a in self.ancestors(n)))

    def after_first(self, boundary: Kind) -> Query:
        """Keep body-statements that follow the first sibling matching ``boundary``.

        The basis for "declarations must precede code" rules: pair with ``directly_inside`` to
        scope it to a module or class body.
        """
        return self.keeping(self.is_late(boundary))

    def violations(self, label: str | Callable[[ast.AST], str]) -> Iterator[Violation]:
        """Turn the surviving nodes into `Violation`s located at each node's line."""
        return (Violation(node.lineno, label(node) if callable(label) else label) for node in self.items)

    def exists(self) -> bool:
        return bool(self.items)

    def keeping(self, predicate: Callable[[ast.AST], bool]) -> Query:
        return replace(self, items=tuple(node for node in self.items if predicate(node)))

    def ancestors(self, node: ast.AST) -> Iterator[ast.AST]:
        cur = self.parents.get(node)
        while cur is not None:
            yield cur
            cur = self.parents.get(cur)

    def is_late(self, boundary: Kind) -> Callable[[ast.AST], bool]:
        def late(node: ast.AST) -> bool:
            body = getattr(self.parents.get(node), "body", None)
            if not isinstance(body, list) or node not in body:
                return False
            cut = next((i for i, stmt in enumerate(body) if boundary.matches(stmt)), None)
            return cut is not None and body.index(node) > cut

        return late

    def __iter__(self) -> Iterator[ast.AST]:
        return iter(self.items)


@dataclass(frozen=True, slots=True)
class Slot:
    """An annotated slot: a variable, function return, or parameter and its annotation expr."""

    name: str
    annotation: ast.expr
    line: int
    is_return: bool = False


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node in ``tree`` to its parent node."""
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def name_of(node: ast.AST) -> str | None:
    """The name a node binds or defines: a class, function, simple assignment target, or argument."""
    match node:
        case ast.ClassDef(name=name) | ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
            return name
        case ast.Assign(targets=[ast.Name(id=name), *_]) | ast.AnnAssign(target=ast.Name(id=name)):
            return name
        case ast.arg(arg=name):
            return name
        case _:
            return None


def is_name(expr: ast.expr, name: str) -> bool:
    """Return whether ``expr`` is exactly the bare name ``name`` (e.g. ``is_name(ann, "Any")``)."""
    return isinstance(expr, ast.Name) and expr.id == name


def has_keyword(call: ast.Call, name: str) -> bool:
    """Return whether ``call`` passes a keyword argument named ``name``."""
    return any(kw.arg == name for kw in call.keywords)


def has_future_annotations(tree: ast.Module) -> bool:
    """Return whether the module begins with ``from __future__ import annotations``."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def annotations(tree: ast.Module) -> Iterator[ast.expr]:
    """Every annotation expression in ``tree`` — annotated assignments, returns, and arguments."""
    for node in ast.walk(tree):
        match node:
            case ast.AnnAssign(annotation=ann):
                yield ann
            case ast.FunctionDef(returns=ann) | ast.AsyncFunctionDef(returns=ann) if ann is not None:
                yield ann
            case ast.arg(annotation=ann) if ann is not None:
                yield ann


def string_literals(expr: ast.expr) -> Iterator[ast.Constant]:
    """String-literal type references within an annotation, skipping ``Literal``/``Annotated`` metadata."""
    match expr:
        case ast.Constant(value=str()):
            yield expr
        case ast.Subscript(value=ast.Name(id="Literal") | ast.Attribute(attr="Literal")):
            return
        case ast.Subscript(value=ast.Name(id="Annotated") | ast.Attribute(attr="Annotated"), slice=ast.Tuple(elts=[first, *_])):
            yield from string_literals(first)
        case _:
            yield from (ref for child in ast.iter_child_nodes(expr) if isinstance(child, ast.expr) for ref in string_literals(child))


def annotated_slots(tree: ast.Module) -> Iterator[Slot]:
    """Every annotated slot in ``tree`` as a [`Slot`][captain_hook.styleguide.Slot].

    Covers annotated variables, function return types, and positional/keyword parameters —
    enough to drive any annotation-shape rule via a predicate on ``slot.annotation``.
    """
    for node in ast.walk(tree):
        match node:
            case ast.AnnAssign(target=ast.Name(id=name), annotation=ann):
                yield Slot(name, ann, node.lineno)
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                if node.returns is not None:
                    yield Slot(node.name, node.returns, node.lineno, is_return=True)
                yield from (
                    Slot(arg.arg, arg.annotation, arg.lineno)
                    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                    if arg.annotation is not None
                )
