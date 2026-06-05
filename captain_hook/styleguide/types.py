from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from captain_hook.utils import kebab

if TYPE_CHECKING:
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

    Subclass it, write the rule's message as the class **docstring** (``{violations}`` is
    substituted at fire time), and implement ``check``. The class name is the rule's identity —
    ``NoNestedImports`` becomes ``"no-nested-imports"``.

    Example:
        ```python
        class NoPrint(StyleRule):
            \"\"\"
            print() calls don't belong in committed code:
              - {violations}
            \"\"\"
            def check(self, tree):
                for node in ast.walk(tree):
                    match node:
                        case ast.Call(func=ast.Name(id="print")):
                            yield Violation(node.lineno, "print() call")
        ```
    """

    trigger: ClassVar[str | None] = None
    sep: ClassVar[str] = "\n  - "
    tests: ClassVar[InlineTests] = {}

    @classmethod
    def slug(cls) -> str:
        return kebab(cls.__name__)

    @abstractmethod
    def check(self, tree: ast.Module) -> Iterator[Violation]: ...


class StyleDiffRule(StyleRule):
    """Base class for a diff rule: flags constructs newly introduced by the change.

    Like [`StyleRule`][captain_hook.styleguide.StyleRule], but ``check`` receives both the
    pre-edit and post-edit module trees, so it can report only what the edit *added*.
    """

    @abstractmethod
    def check(self, pre: ast.Module, post: ast.Module) -> Iterator[Violation]: ...  # type: ignore[override]
