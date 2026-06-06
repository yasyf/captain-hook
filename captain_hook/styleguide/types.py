from __future__ import annotations

import ast
from abc import ABC
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from captain_hook.utils import kebab

if TYPE_CHECKING:
    from captain_hook.styleguide.matcher import Matcher
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


class StyleRule(ABC):
    """Base class for a single-tree AST style rule applied to Python edits and writes.

    Subclass it and write the rule's message as the class **docstring** (``{violations}`` is
    substituted at fire time). Declare the rule as data by setting ``match`` to a
    [`Matcher`][captain_hook.styleguide.Matcher] (and optionally ``label``); override ``check``
    only for logic a matcher can't express. The class name is the rule's identity —
    ``NoNestedImports`` becomes ``"no-nested-imports"``.

    Example:
        ```python
        class NoNestedImports(StyleRule):
            \"\"\"Lazy imports belong at the top of the function body: {violations}\"\"\"

            match = Matcher.imports & Matcher.child_of(Matcher.control_flow) & ~Matcher.under(Matcher.type_checking)
        ```
    """

    trigger: ClassVar[str | None] = None
    sep: ClassVar[str] = "\n  - "
    tests: ClassVar[InlineTests] = {}
    match: ClassVar[Matcher | None] = None
    label: ClassVar[str | Callable[[ast.AST], str] | None] = None

    @classmethod
    def slug(cls) -> str:
        return kebab(cls.__name__)

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        if (matcher := type(self).match) is not None:
            yield from matcher.violations(tree, type(self).label)


class StyleDiffRule(StyleRule):
    """Base class for a diff rule: flags constructs newly introduced by the change.

    Like [`StyleRule`][captain_hook.styleguide.StyleRule], but it compares the pre-edit and
    post-edit trees. The declarative form flags nodes matching ``match`` in the new tree whose
    ``key`` (default ``ast.unparse``) was absent from the old tree; override ``check`` when the
    "newly introduced" identity needs custom logic.

    Example:
        ```python
        class NoNewWildcardImport(StyleDiffRule):
            \"\"\"Wildcard import added by this edit: {violations}\"\"\"

            match = Matcher.imports.where(lambda n: any(a.name == "*" for a in n.names))
        ```
    """

    key: ClassVar[Callable[[ast.AST], Hashable] | None] = None

    def check(self, pre: ast.Module, post: ast.Module) -> Iterator[Violation]:  # type: ignore[override]
        if (matcher := type(self).match) is not None:
            yield from matcher.diff(pre, post, type(self).key or ast.unparse, type(self).label)
