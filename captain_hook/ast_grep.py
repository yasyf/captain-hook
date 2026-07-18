"""The only module that imports ``ast_grep_py``: structural code search and rewriting by language.

Everything else in the framework reaches ast-grep through here — the :class:`~captain_hook.types.Pattern`
condition, the ``ast_grep_rule`` style builders, and the ``rewrite_code`` primitive — so the binding
lives behind one seam.

Patterns are plain ast-grep pattern strings with metavariables: ``print($$$)`` matches a print call
however its arguments are spelled, ``os.system($CMD)`` captures the argument as ``$CMD``. Rewrites
reuse ast-grep's ``$VAR`` / ``$$$VAR`` fix syntax. Language ids are the same short keys as
:data:`~captain_hook.langs.LANG_GLOBS` (``"py"``, ``"go"``, ``"ts"``, ...); ast-grep also accepts the
long names (``"python"``).
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.langs import COMMENT_TYPES as GENERATED_COMMENT_TYPES
from captain_hook.langs import LANG_GLOBS

if TYPE_CHECKING:
    from pathlib import Path

    from ast_grep_py import SgNode

EXT_TO_LANG: dict[str, str] = {glob.removeprefix("*."): lang for lang, globs in LANG_GLOBS.items() for glob in globs}

TEMPLATE_VAR = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)|\$([A-Z_][A-Z0-9_]*)")

COMMENT_TYPES: frozenset[str] = GENERATED_COMMENT_TYPES
"""Tree-sitter node kinds that denote a comment, across every supported grammar.

The union covers every [`LANG_GLOBS`][captain_hook.langs.LANG_GLOBS] grammar. Markdown (``md``) is
the verified exception with no comment kinds. A Dart block documentation comment has an outer
``comment`` with a nested ``documentation_block_comment``; its grammar has no literal
``documentation_comment`` kind. Generation fails if another supported grammar defines none of the
known comment container kinds.
"""

MAX_COMMENT_LINES = 3
MAX_COMMENT_CHARS = 200
MAX_COMMENT_SCAN_BYTES = 512_000

DOC_PREFIXES: dict[str, tuple[str, ...]] = {
    "rs": ("///", "//!", "/**", "/*!"),
    "js": ("/**",),
    "jsx": ("/**",),
    "ts": ("/**",),
    "tsx": ("/**",),
    "java": ("/**",),
    "kotlin": ("/**",),
    "swift": ("///", "/**"),
    "dart": ("///",),
    "cs": ("///",),
    "php": ("/**",),
    "scala": ("/**",),
}
"""Comment prefixes that mark a documentation comment, by language."""

GO_DOC_SIBLINGS: frozenset[str] = frozenset(
    {
        "function_declaration",
        "method_declaration",
        "type_declaration",
        "var_declaration",
        "const_declaration",
        "package_clause",
        "const_spec",
        "var_spec",
        "field_declaration",
        "import_declaration",
        "method_elem",
    }
)
"""Kinds a Go run must immediately precede to read as godoc — top-level declarations plus grouped
``const``/``var`` specs, struct fields, imports, and interface methods (which nest below the file)."""


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
    return EXT_TO_LANG.get(path.suffix.removeprefix(".").lower())


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


@dataclass(frozen=True, slots=True)
class CommentRun:
    """A maximal run of adjacent line-leading comments (or one block comment), located by 1-based
    line span and classified doc vs inline. A trailing comment (code before it on the line) is
    always a singleton run and never groups with its neighbours."""

    line: int
    end_line: int
    texts: tuple[str, ...]
    doc: bool
    leading: bool

    @property
    def lines(self) -> int:
        return self.end_line - self.line + 1

    @property
    def chars(self) -> int:
        return sum(len(t.rstrip("\n")) for t in self.texts)

    @property
    def key(self) -> str:
        """Whitespace-normalized joined text: a run's identity across reflow and reindent."""
        return " ".join("\n".join(self.texts).split())


@dataclass(frozen=True, slots=True)
class CommentBlock:
    """One or more comment runs a size check treats as a unit: adjacent line-leading runs and the
    paragraphs they form across blank-only gaps. ``lines``/``chars`` sum the constituent runs (the
    blank gaps don't count), and the block is doc only when every run is."""

    runs: tuple[CommentRun, ...]

    @property
    def line(self) -> int:
        return self.runs[0].line

    @property
    def end_line(self) -> int:
        return self.runs[-1].end_line

    @property
    def lines(self) -> int:
        return sum(run.lines for run in self.runs)

    @property
    def chars(self) -> int:
        return sum(run.chars for run in self.runs)

    @property
    def doc(self) -> bool:
        return all(run.doc for run in self.runs)

    @property
    def key(self) -> str:
        return " ".join("\n".join(t for run in self.runs for t in run.texts).split())

    @property
    def too_long(self) -> bool:
        return self.lines > MAX_COMMENT_LINES or self.chars > MAX_COMMENT_CHARS


def line_span(node: SyntaxNode) -> tuple[int, int]:
    """1-based line span of a comment's text — trailing newlines excluded, so a grammar that folds
    the newline into the node (rust ``///``) doesn't inflate the run onto the code line below it."""
    start = node.raw.range().start.line + 1
    return start, start + node.text.rstrip("\n").count("\n")


def is_shebang(node: SyntaxNode) -> bool:
    """Whether a comment node is a ``#!`` shebang on the first line — not prose, not run material."""
    return node.raw.range().start.line == 0 and node.text.startswith("#!")


def is_line_leading(node: SyntaxNode, src_lines: list[str]) -> bool:
    """Whether nothing but whitespace precedes the comment on its line (vs a trailing ``code  # …``)."""
    r = node.raw.range()
    line = src_lines[r.start.line] if r.start.line < len(src_lines) else ""
    return not line[: r.start.column].strip()


def is_doc_run(nodes: list[SyntaxNode], lang: str) -> bool:
    """Whether a comment run is a documentation comment (godoc / rustdoc / JSDoc).

    Prefix languages (rustdoc ``///``, JSDoc ``/**``) require *every* node to carry a doc marker, so
    a marker line followed by plain narrative doesn't poison the run doc. Go has no prefix: a run is
    godoc when its next named sibling — a declaration, grouped spec, struct field, import, or
    interface method — begins on the immediately following line (a blank line breaks the bond).
    """
    if prefixes := DOC_PREFIXES.get(lang):
        return all(n.text.startswith(prefixes) for n in nodes)
    if lang != "go":
        return False
    last = nodes[-1].raw
    if (nxt := last.next()) is None or nxt.kind() not in GO_DOC_SIBLINGS:
        return False
    return nxt.range().start.line == last.range().end.line + 1


@functools.lru_cache(maxsize=8)
def comment_runs(source: str, lang: str) -> list[CommentRun]:
    """Every comment run in ``source``, grouped by line adjacency and doc-classified.

    Only line-leading comments group; a trailing comment and a ``#!`` shebang each stand alone (the
    shebang is dropped entirely). Cached per ``(source, lang)`` — several conditions parse the same
    image per event.
    """
    # Bound hook latency by skipping comment scans for large sources.
    if len(source.encode()) > MAX_COMMENT_SCAN_BYTES:
        return []
    src_lines = source.splitlines()
    groups: list[tuple[list[SyntaxNode], bool]] = []
    last_comment_end = (-1, -1)
    for node in parse(source, lang).descendants():
        if node.kind not in COMMENT_TYPES:
            continue
        node_range = node.raw.range()
        if (node_range.start.line, node_range.start.column) < last_comment_end:
            continue
        last_comment_end = (node_range.end.line, node_range.end.column)
        if is_shebang(node):
            continue
        leading = is_line_leading(node, src_lines)
        if leading and groups and groups[-1][1] and line_span(node)[0] <= line_span(groups[-1][0][-1])[1] + 1:
            groups[-1][0].append(node)
        else:
            groups.append(([node], leading))
    return [
        CommentRun(
            line=line_span(nodes[0])[0],
            end_line=line_span(nodes[-1])[1],
            texts=tuple(n.text for n in nodes),
            doc=is_doc_run(nodes, lang),
            leading=leading,
        )
        for nodes, leading in groups
    ]


def comment_blocks(source: str, lang: str) -> list[CommentBlock]:
    """Comment runs merged into blocks for the size check: consecutive line-leading runs separated
    only by blank lines (no code between) form one block, so blank-line-split paragraphs count as a
    whole. Trailing runs stay their own block."""
    src_lines = source.splitlines()
    blocks: list[list[CommentRun]] = []
    for run in comment_runs(source, lang):
        prev = blocks[-1][-1] if blocks else None
        if (
            run.leading
            and prev is not None
            and prev.leading
            and all(not src_lines[i - 1].strip() for i in range(prev.end_line + 1, run.line))
        ):
            blocks[-1].append(run)
        else:
            blocks.append([run])
    return [CommentBlock(runs=tuple(runs)) for runs in blocks]


def touched_comment_blocks(old: str, new: str, lang: str) -> list[CommentBlock]:
    """Comment blocks of ``new`` this edit created or grew — a multiset diff over :attr:`CommentBlock.key`.

    An old block exempts one identically-keyed new block (a genuine move stays exempt; a duplicated
    copy past the first counts as touched). A too-long new block is exempt only when an old block
    with the same key was *also* too long, so a legacy oversized run stays quiet while a reflow that
    pushes a previously-fine run over the threshold does not.
    """
    old_blocks = comment_blocks(old, lang)
    remaining = Counter(block.key for block in old_blocks)
    old_too_long = {block.key for block in old_blocks if block.too_long}
    touched: list[CommentBlock] = []
    for block in comment_blocks(new, lang):
        if remaining[block.key] > 0:
            remaining[block.key] -= 1
            if block.too_long and block.key not in old_too_long:
                touched.append(block)
        else:
            touched.append(block)
    return touched


def comment_line_numbers(source: str, lang: str, *, include_doc: bool) -> set[int]:
    """The 1-based line numbers occupied by line-leading comments in ``source`` (trailing comments
    sit on code lines, so they don't count); ``include_doc`` keeps doc runs."""
    return {
        line
        for run in comment_runs(source, lang)
        if run.leading and (include_doc or not run.doc)
        for line in range(run.line, run.end_line + 1)
    }


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
