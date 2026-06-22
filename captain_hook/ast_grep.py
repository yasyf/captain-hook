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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.types import LANG_GLOBS

if TYPE_CHECKING:
    from pathlib import Path

    from ast_grep_py import SgNode

EXT_TO_LANG: dict[str, str] = {glob.removeprefix("*."): lang for lang, globs in LANG_GLOBS.items() for glob in globs}

TEMPLATE_VAR = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)|\$([A-Z_][A-Z0-9_]*)")


@dataclass(frozen=True, slots=True)
class Match:
    """A structural match, located by 1-based line to align with ``Violation`` and changed-line scoping."""

    line: int
    end_line: int
    text: str


def lang_for_path(path: Path) -> str | None:
    """Infer an ast-grep language id from a file extension, or ``None`` when unsupported."""
    return EXT_TO_LANG.get(path.suffix.removeprefix("."))


def has_metavar(text: str) -> bool:
    """Whether ``text`` carries an ast-grep metavariable (``$NAME`` or ``$$$NAME``)."""
    return TEMPLATE_VAR.search(text) is not None


def parse(source: str, lang: str) -> SgNode:
    from ast_grep_py import SgRoot

    return SgRoot(source, lang).root()


def to_match(node: SgNode) -> Match:
    r = node.range()
    return Match(line=r.start.line + 1, end_line=r.end.line + 1, text=node.text())


def match_key(m: Match) -> str:
    return " ".join(m.text.split())


def matches(source: str, lang: str, pattern: str) -> bool:
    """Whether ``pattern`` matches anywhere in ``source`` — the cheap boolean for conditions."""
    return parse(source, lang).find(pattern=pattern) is not None


def find_all(source: str, lang: str, pattern: str) -> Iterator[Match]:
    """Every structural match of ``pattern`` in ``source``, as 1-based-line :class:`Match` objects."""
    return (to_match(node) for node in parse(source, lang).find_all(pattern=pattern))


def find_introduced(old: str, new: str, lang: str, pattern: str) -> Iterator[Match]:
    """Matches present in ``new`` whose construct was absent from ``old`` — the diff helper.

    Identity is the match's whitespace-normalized text, not its range (which shifts as
    surrounding code moves) — so a pre-existing construct is never reported as newly added.
    """
    before = {match_key(m) for m in find_all(old, lang, pattern)}
    return (m for m in find_all(new, lang, pattern) if match_key(m) not in before)


def rewrite(source: str, lang: str, pattern: str, replace: str) -> str:
    """Rewrite every ``pattern`` match in ``source`` to ``replace``, an ast-grep fix template.

    ``replace`` uses ast-grep's ``$VAR`` / ``$$$VAR`` fix syntax, each metavariable filled from the
    match it names. Returns ``source`` unchanged when nothing matches.
    """
    root = parse(source, lang)
    if not (edits := [node.replace(fill_template(node, replace)) for node in root.find_all(pattern=pattern)]):
        return source
    return root.commit_edits(edits)


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
