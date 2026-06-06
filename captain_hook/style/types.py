from __future__ import annotations

import ast
from abc import ABC
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        if (matcher := type(self).match) is not None:
            yield from matcher.violations(tree, type(self).label)


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

    def check(self, pre: ast.Module, post: ast.Module) -> Iterator[Violation]:  # type: ignore[override]
        if (matcher := type(self).match) is not None:
            yield from matcher.diff(pre, post, label=type(self).label)
