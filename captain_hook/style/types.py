from __future__ import annotations

import ast
from abc import ABC
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar

from captain_hook.utils import kebab

if TYPE_CHECKING:
    from captain_hook.style.matchers import Matcher
    from captain_hook.testing import InlineTests


@dataclass(frozen=True, slots=True)
class Violation:
    """A single style violation, located by line so the runner can scope it to the edit.

    Attributes:
        line: 1-based line number of the offending construct in the post-edit file.
        label: Short human-readable description, rendered as ``"{label} (line {line})"``.
    """

    line: int
    label: str


@dataclass(frozen=True)
class Change:
    """The pre- and post-edit state of a file, passed to every style rule's ``check``.

    Attributes:
        source: The post-edit source text.
        pre: The pre-edit source text (``""`` when there is nothing to diff against).
        path: The post-edit file's path as a string, so rules can inspect the filename.
        tree: The parsed post-edit module (an empty module when the source doesn't parse).
        pre_tree: The parsed pre-edit module (parsed lazily, only when a diff rule reads it).
    """

    source: str
    pre: str
    path: str

    @staticmethod
    def parse_or_empty(source: str) -> ast.Module:
        try:
            return ast.parse(source)
        except SyntaxError:
            return ast.parse("")

    @cached_property
    def tree(self) -> ast.Module:
        return self.parse_or_empty(self.source)

    @cached_property
    def pre_tree(self) -> ast.Module:
        return self.parse_or_empty(self.pre)


class StyleRule(ABC):
    """Base class for a single-tree AST style rule applied to Python edits and writes.

    Subclass it and write the rule's message as the class **docstring** (``{violations}`` is
    substituted at fire time). Declare the rule as data by setting ``match`` to a
    [`Matcher`][captain_hook.style.matchers.Matcher] (and optionally ``label``); override ``check``
    only for logic a matcher can't express. The class name is the rule's identity —
    ``NoNestedImports`` becomes ``"no-nested-imports"``.

    Example:
        ```python
        from captain_hook.style import matchers as M

        class NoNestedImports(StyleRule):
            \"\"\"Lazy imports belong at the top of the function body: {violations}\"\"\"

            match = M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)
        ```
    """

    sep: ClassVar[str] = "\n  - "
    tests: ClassVar[InlineTests] = {}
    match: ClassVar[Matcher | None] = None
    label: ClassVar[str | Callable[[ast.AST], str] | None] = None

    @classmethod
    def slug(cls) -> str:
        return kebab(cls.__name__)

    def check(self, change: Change) -> Iterator[Violation]:
        if (matcher := type(self).match) is not None:
            yield from matcher.violations(change.tree, type(self).label)


class StyleDiffRule(StyleRule):
    """Base class for a diff rule: flags constructs newly introduced by the change.

    Like [`StyleRule`][captain_hook.style.StyleRule], but it compares the pre-edit and
    post-edit trees. The declarative form flags nodes matching ``match`` in the new tree
    that were absent from the old tree (by unparsed source); override ``check`` when the
    "newly introduced" identity needs custom logic.

    Example:
        ```python
        from captain_hook.style import matchers as M

        class NoNewWildcardImport(StyleDiffRule):
            \"\"\"Wildcard import added by this edit: {violations}\"\"\"

            match = M.imports.where(lambda n: any(a.name == "*" for a in n.names))
        ```
    """

    def check(self, change: Change) -> Iterator[Violation]:
        if (matcher := type(self).match) is not None:
            yield from matcher.diff(change.pre_tree, change.tree, label=type(self).label)
