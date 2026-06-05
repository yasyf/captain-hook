from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from typing import ClassVar


class SourceLints:
    UNDERSCORE_PATTERNS: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(r"^\s*class\s+(_[A-Za-z][A-Za-z0-9_]*)"),
        re.compile(r"^\s*(_[A-Z][A-Z_0-9]*)\s*[=:]"),
    )

    CONTROL_FLOW_NODES: ClassVar[tuple[type[ast.AST], ...]] = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.ExceptHandler,
    )

    @classmethod
    def underscore_prefixes(cls, content: str) -> list[str]:
        return [
            m.group(1)
            for line in content.splitlines()
            for pat in cls.UNDERSCORE_PATTERNS
            if (m := pat.match(line)) and not m.group(1).startswith("__")
        ]

    @classmethod
    def nested_imports(cls, content: str) -> list[str]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        return [
            ast.unparse(imp)
            for imp in ast.walk(tree)
            if isinstance(imp, (ast.Import, ast.ImportFrom))
            and isinstance(parents.get(imp), cls.CONTROL_FLOW_NODES)
            and not cls.under_type_checking(imp, parents)
        ]

    @classmethod
    def under_type_checking(cls, node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.If) and cls.is_type_checking_test(cur.test):
                return True
            cur = parents.get(cur)
        return False

    @staticmethod
    def is_type_checking_test(test: ast.expr) -> bool:
        match test:
            case ast.Name(id="TYPE_CHECKING"):
                return True
            case ast.Attribute(value=ast.Name(id="typing"), attr="TYPE_CHECKING"):
                return True
            case _:
                return False


class AstLints:
    UPPER_SNAKE: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]+$")

    MODULE_BOUNDARY: ClassVar[tuple[type[ast.AST], ...]] = (
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )

    CLASS_BOUNDARY: ClassVar[tuple[type[ast.AST], ...]] = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )

    @staticmethod
    def zip_without_strict(node: ast.AST) -> Iterator[str]:
        match node:
            case ast.Call(func=ast.Name(id="zip"), keywords=kws) if not any(kw.arg == "strict" for kw in kws):
                yield f"zip() at line {node.lineno}"
            case _:
                pass

    @classmethod
    def late_module_constants(cls, node: ast.AST) -> Iterator[str]:
        if isinstance(node, ast.Module):
            yield from cls.late_assignments(
                node.body,
                boundary=cls.MODULE_BOUNDARY,
                keep=lambda n: bool(cls.UPPER_SNAKE.match(n)),
                fmt=lambda n, s: f"{n} at line {s.lineno}",
            )

    @classmethod
    def late_class_constants(cls, node: ast.AST) -> Iterator[str]:
        if isinstance(node, ast.ClassDef):
            yield from cls.late_assignments(
                node.body,
                boundary=cls.CLASS_BOUNDARY,
                keep=lambda _: True,
                fmt=lambda n, s: f"{node.name}.{n} at line {s.lineno}",
            )

    @classmethod
    def late_assignments(
        cls,
        body: list[ast.stmt],
        *,
        boundary: tuple[type[ast.AST], ...],
        keep: Callable[[str], bool],
        fmt: Callable[[str, ast.stmt], str],
    ) -> Iterator[str]:
        if (idx := next((i for i, s in enumerate(body) if isinstance(s, boundary)), None)) is None:
            return
        yield from (
            fmt(name, stmt) for stmt in body[idx + 1 :] if (name := cls.assignment_name(stmt)) and keep(name)
        )

    @staticmethod
    def assignment_name(stmt: ast.stmt) -> str | None:
        match stmt:
            case ast.Assign(targets=[ast.Name(id=name), *_]):
                return name
            case ast.AnnAssign(target=ast.Name(id=name)):
                return name
            case _:
                return None

    @classmethod
    def quoted_annotations_under_future(cls, node: ast.AST) -> Iterator[str]:
        if not isinstance(node, ast.Module) or not cls.has_future_annotations(node):
            return
        yield from sorted(
            {
                f"{const.value!r} at line {const.lineno}"
                for ann in cls.iter_annotation_nodes(node)
                for const in cls.find_string_type_refs(ann)
            }
        )

    @staticmethod
    def has_future_annotations(tree: ast.Module) -> bool:
        return any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )

    @classmethod
    def iter_annotation_nodes(cls, tree: ast.Module) -> Iterator[ast.expr]:
        return (ann for node in ast.walk(tree) if (ann := cls.annotation_of(node)) is not None)

    @staticmethod
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

    @classmethod
    def find_string_type_refs(cls, node: ast.expr) -> Iterator[ast.Constant]:
        match node:
            case ast.Constant(value=str()):
                yield node
            case ast.Subscript(value=ast.Name(id="Literal") | ast.Attribute(attr="Literal")):
                return
            case ast.Subscript(
                value=ast.Name(id="Annotated") | ast.Attribute(attr="Annotated"),
                slice=ast.Tuple(elts=[first, *_]),
            ):
                yield from cls.find_string_type_refs(first)
            case _:
                yield from (
                    ref
                    for child in ast.iter_child_nodes(node)
                    if isinstance(child, ast.expr)
                    for ref in cls.find_string_type_refs(child)
                )


class AntiAnyLints:
    @classmethod
    def weakened_to_any(cls, old: ast.AST, new: ast.AST) -> list[str]:
        return sorted(cls.any_annotations(new) - cls.any_annotations(old))

    @classmethod
    def any_annotations(cls, tree: ast.AST) -> set[str]:
        return {label for node in ast.walk(tree) for label in cls.any_labels(node)}

    @classmethod
    def any_labels(cls, node: ast.AST) -> Iterator[str]:
        match node:
            case ast.AnnAssign(target=ast.Name(id=name), annotation=ann) if cls.is_any(ann):
                yield f"{name}: Any"
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                if node.returns and cls.is_any(node.returns):
                    yield f"{node.name}() -> Any"
                yield from (
                    f"{arg.arg}: Any"
                    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                    if arg.annotation and cls.is_any(arg.annotation)
                )

    @staticmethod
    def is_any(ann: ast.expr) -> bool:
        return isinstance(ann, ast.Name) and ann.id == "Any"
