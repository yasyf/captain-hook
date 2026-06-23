"""ast-grep-backed style rules: author a rule from an inline pattern instead of a Matcher subclass.

[`ast_grep_rule`][captain_hook.style.ast_grep_rule] and
[`ast_grep_diff_rule`][captain_hook.style.ast_grep_diff_rule] build ordinary
[`StyleRule`][captain_hook.style.StyleRule] subclasses, so they register with
[`styleguide`][captain_hook.style.styleguide] and reuse its change-scoping, diffing, and message
formatting — only the matcher is an ast-grep pattern instead of the Python ``ast`` walker, which also
makes them work for languages other than Python.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, ClassVar, Self

from captain_hook.ast_grep import find_all, find_introduced
from captain_hook.style.types import Change, StyleDiffRule, StyleRule, Violation

if TYPE_CHECKING:
    from captain_hook.ast_grep import Match
    from captain_hook.testing import InlineTests


class AstGrepStyleRule(StyleRule):
    """A [`StyleRule`][captain_hook.style.StyleRule] matched by an ast-grep pattern over source text.

    Built by [`ast_grep_rule`][captain_hook.style.ast_grep_rule]; the engine runs ``pattern`` against
    the edited source in ``lang`` and scopes each match to the changed lines.
    """

    pattern: ClassVar[str]
    lang: ClassVar[str]

    def check(self, change: Change) -> Iterator[Violation]:
        return (
            Violation(m.line, self.label_of(m)) for m in find_all(change.source, type(self).lang, type(self).pattern)
        )

    def label_of(self, m: Match) -> str:
        return label if isinstance(label := type(self).label, str) else m.text.splitlines()[0]

    @classmethod
    def configure(
        cls,
        name: str,
        pattern: str,
        message: str,
        lang: str,
        label: str | None,
        tests: InlineTests | None,
    ) -> type[Self]:
        cls.__name__ = cls.__qualname__ = name
        cls.__doc__ = message
        cls.pattern = pattern
        cls.lang = lang
        cls.label = label
        cls.tests = tests or {}
        return cls


class AstGrepStyleDiffRule(AstGrepStyleRule, StyleDiffRule):
    """An [`AstGrepStyleRule`][captain_hook.style.AstGrepStyleRule] that flags only constructs the edit introduces."""

    def check(self, change: Change) -> Iterator[Violation]:
        return (
            Violation(m.line, self.label_of(m))
            for m in find_introduced(change.pre, change.source, type(self).lang, type(self).pattern)
        )


def ast_grep_rule(
    name: str,
    *,
    pattern: str,
    message: str,
    lang: str = "py",
    label: str | None = None,
    tests: InlineTests | None = None,
) -> type[AstGrepStyleRule]:
    """Build a [`StyleRule`][captain_hook.style.StyleRule] from an inline ast-grep pattern.

    ``pattern`` is an ast-grep pattern string with metavariables (``print($$$)``). ``message`` is the
    rule's message (``{violations}`` is substituted at fire time); ``label`` names each match,
    defaulting to the matched code. Register the result with
    [`styleguide`][captain_hook.style.styleguide].

    Example:
        >>> NoPrint = ast_grep_rule("NoPrint", pattern="print($$$)", message="No print(): {violations}")
        >>> styleguide(NoPrint)
    """

    class Cls(AstGrepStyleRule):
        pass

    return Cls.configure(name, pattern, message, lang, label, tests)


def ast_grep_diff_rule(
    name: str,
    *,
    pattern: str,
    message: str,
    lang: str = "py",
    label: str | None = None,
    tests: InlineTests | None = None,
) -> type[AstGrepStyleDiffRule]:
    """Like [`ast_grep_rule`][captain_hook.style.ast_grep_rule], but flags only matches the edit newly introduces.

    Example:
        >>> NoNewWildcard = ast_grep_diff_rule("NoNewWildcard", pattern="from $M import *", message="No `import *`")
    """

    class Cls(AstGrepStyleDiffRule):
        pass

    return Cls.configure(name, pattern, message, lang, label, tests)
