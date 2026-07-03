"""The only module that imports ``ast_grep_py``: structural code search and rewriting by language.

Everything else in the framework reaches ast-grep through here — the :class:`~captain_hook.types.Pattern`
condition, the ``ast_grep_rule`` style builders, and the ``rewrite_code`` primitive — so the binding
lives behind one seam.

Patterns are plain ast-grep pattern strings with metavariables: ``print($$$)`` matches a print call
however its arguments are spelled, ``os.system($CMD)`` captures the argument as ``$CMD``. Rewrites
reuse ast-grep's ``$VAR`` / ``$$$VAR`` fix syntax. Language ids are the same short keys as
:data:`~captain_hook.types.LANG_GLOBS` (``"py"``, ``"go"``, ``"ts"``, ...); ast-grep also accepts the
long names (``"python"``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.types import LANG_GLOBS

if TYPE_CHECKING:
    from pathlib import Path

    from ast_grep_py import SgNode

EXT_TO_LANG: dict[str, str] = {glob.removeprefix("*."): lang for lang, globs in LANG_GLOBS.items() for glob in globs}

TEMPLATE_VAR = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)|\$([A-Z_][A-Z0-9_]*)")

COMMENT_TYPES: frozenset[str] = frozenset({"comment", "line_comment", "block_comment"})
"""Tree-sitter node kinds that denote a comment, across every supported grammar.

The union covers every [`LANG_GLOBS`][captain_hook.types.LANG_GLOBS] grammar; a future grammar
whose top-level comment kind is named differently would silently miss.
"""


@dataclass(frozen=True, slots=True)
class Match:
    """A structural match, located by 1-based line to align with ``Violation`` and changed-line scoping."""

    line: int
    end_line: int
    text: str


@dataclass(frozen=True, slots=True)
class SyntaxNode:
    """One node of a parsed syntax tree — the framework's face over the ast-grep binding node."""

    raw: SgNode

    @property
    def kind(self) -> str:
        return self.raw.kind()

    @property
    def text(self) -> str:
        return self.raw.text()

    def descendants(self) -> Iterator[SyntaxNode]:
        """Every node below this one, in document order."""
        for raw in self.raw.children():
            yield (child := SyntaxNode(raw))
            yield from child.descendants()

    def to_match(self) -> Match:
        r = self.raw.range()
        return Match(line=r.start.line + 1, end_line=r.end.line + 1, text=self.raw.text())


def lang_for_path(path: Path) -> str | None:
    """Infer an ast-grep language id from a file extension, or ``None`` when unsupported."""
    return EXT_TO_LANG.get(path.suffix.removeprefix("."))


def has_metavar(text: str) -> bool:
    """Whether ``text`` carries an ast-grep metavariable (``$NAME`` or ``$$$NAME``)."""
    return TEMPLATE_VAR.search(text) is not None


def parse(source: str, lang: str) -> SyntaxNode:
    from ast_grep_py import SgRoot

    return SyntaxNode(SgRoot(source, lang).root())


def match_key(m: Match) -> str:
    return " ".join(m.text.split())


def matches(source: str, lang: str, pattern: str) -> bool:
    """Whether ``pattern`` matches anywhere in ``source`` — the cheap boolean for conditions."""
    return parse(source, lang).raw.find(pattern=pattern) is not None


def find_all(source: str, lang: str, pattern: str) -> Iterator[Match]:
    """Every structural match of ``pattern`` in ``source``, as 1-based-line :class:`Match` objects."""
    return (SyntaxNode(node).to_match() for node in parse(source, lang).raw.find_all(pattern=pattern))


def find_kinds(source: str, lang: str, kinds: Set[str]) -> Iterator[Match]:
    """Every node whose tree-sitter kind is in ``kinds``, in document order.

    Kinds are read off parsed nodes rather than passed to the ast-grep kind
    matcher, which raises on kind names a grammar doesn't define.
    """
    return (n.to_match() for n in parse(source, lang).descendants() if n.kind in kinds)


def comments(source: str, lang: str) -> Iterator[Match]:
    """Every comment in ``source``, in document order — regardless of language."""
    return find_kinds(source, lang, COMMENT_TYPES)


def introduced(old: Iterable[Match], new: Iterable[Match]) -> Iterator[Match]:
    """Matches in ``new`` whose construct was absent from ``old``.

    Identity is the match's whitespace-normalized text, not its range (which shifts as
    surrounding code moves) — so a pre-existing construct is never reported as newly added.
    """
    before = {match_key(m) for m in old}
    return (m for m in new if match_key(m) not in before)


def find_introduced(old: str, new: str, lang: str, pattern: str) -> Iterator[Match]:
    """Matches of ``pattern`` present in ``new`` but absent from ``old`` — the diff helper."""
    return introduced(find_all(old, lang, pattern), find_all(new, lang, pattern))


def introduced_comments(old: str, new: str, lang: str) -> Iterator[Match]:
    """Comments present in ``new`` whose text was absent from ``old``."""
    return introduced(comments(old, lang), comments(new, lang))


def rewrite(source: str, lang: str, pattern: str, replace: str) -> str:
    """Rewrite every ``pattern`` match in ``source`` to ``replace``, an ast-grep fix template.

    ``replace`` uses ast-grep's ``$VAR`` / ``$$$VAR`` fix syntax, each metavariable filled from the
    match it names. Returns ``source`` unchanged when nothing matches.
    """
    root = parse(source, lang).raw
    if not (edits := [node.replace(fill_template(node, replace)) for node in root.find_all(pattern=pattern)]):
        return source
    return root.commit_edits(edits)


def capture(source: str, lang: str, pattern: str) -> dict[str, str] | None:
    """Match ``pattern`` against ``source`` and extract its named metavars, or ``None`` when it doesn't match.

    Each ``$NAME`` in the pattern maps to the matched node's text; each ``$$$NAME`` maps to the
    original-source span covering its matches, so whitespace is preserved (mirroring :func:`fill_template`).
    A pattern with no metavars that still matches yields an empty dict — present but empty.
    """
    if (node := parse(source, lang).raw.find(pattern=pattern)) is None:
        return None

    def value(m: re.Match[str]) -> str:
        if (name := m.group(1)) is not None:
            if not (spans := node.get_multiple_matches(name)):
                return ""
            full = node.get_root().root().text()
            return full[spans[0].range().start.index : spans[-1].range().end.index]
        return single.text() if (single := node.get_match(m.group(2))) else ""

    return {(m.group(1) or m.group(2)): value(m) for m in TEMPLATE_VAR.finditer(pattern)}


def fill_template(node: SgNode, template: str) -> str:
    """Fill an ast-grep fix ``template`` against one match: ``$NAME`` becomes the metavar's text;
    ``$$$NAME`` becomes the original-source span covering its matches, so whitespace is preserved.

    A ``$NAME`` the pattern never captured is left untouched, so literal ``$VAR`` text in a
    replacement — a shell variable like ``$HOME``, say — passes through unchanged.
    """

    def substitute(m: re.Match[str]) -> str:
        if (name := m.group(1)) is not None:
            if not (spans := node.get_multiple_matches(name)):
                return ""
            source = node.get_root().root().text()
            return source[spans[0].range().start.index : spans[-1].range().end.index]
        return single.text() if (single := node.get_match(m.group(2))) else m.group(0)

    return TEMPLATE_VAR.sub(substitute, template)
