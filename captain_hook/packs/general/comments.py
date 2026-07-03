"""Discourage verbose comments: warn when an edit introduces a long comment or line-comment run.

Comments should be terse and used sparingly — names, types, and organization carry the
meaning. The one legitimate exception is documentation-generation comments (godoc, rustdoc,
docstrings), and even a long doc run trips this warn by design: the threshold is deliberately
strict so verbosity of any kind gets a nudge. Language-agnostic via the tree-sitter comment
kinds in :data:`~captain_hook.ast_grep.COMMENT_TYPES`; diff-based, so only comments the edit
*introduces* count — a pre-existing long comment re-saved unchanged never fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    BaseHookEvent,
    CustomCondition,
    Event,
    FileFixture,
    Input,
    Tool,
    Warn,
    nudge,
)
from captain_hook.ast_grep import introduced_comments, lang_for_path

if TYPE_CHECKING:
    from collections.abc import Iterable

    from captain_hook.ast_grep import Match

MAX_COMMENT_LINES = 4
MAX_COMMENT_CHARS = 300

# Fixtures for the inline tests — kept as module constants so each physical line stays short.
GO_NO_COMMENT = "package p\n\nfunc F() {}\n"
GO_ONE_LINE_DOC = "package p\n\n// F does a thing.\nfunc F() {}\n"
GO_THREE_LINE_DOC = (
    "package p\n\n// F does a thing.\n// It handles the empty case.\n// Returns an error otherwise.\nfunc F() {}\n"
)
GO_LONG_RUN = (
    "package p\n\nfunc F() {\n"
    "\t// explanation line one goes here\n"
    "\t// explanation line two goes here\n"
    "\t// explanation line three is here\n"
    "\t// explanation line four is here\n"
    "\t// explanation line five is here\n"
    "\t// explanation line six is here ok\n"
    "\tx := 1\n}\n"
)
GO_EXISTING_OLD = "func F() {\n\t// aaa\n\t// bbb\n\t// ccc\n\t// ddd\n\t// eee\n\t// fff\n\tx := 1\n}\n"
GO_EXISTING_NEW = "func F() {\n\t// aaa\n\t// bbb\n\t// ccc\n\t// ddd\n\t// eee\n\t// fff\n\tx := 2\n}\n"
GO_WRITE_PLAIN = "package p\n\nvar x = 1\n"
GO_WRITE_RUN = (
    "package p\n\n"
    "// note one here\n// note two here\n// note three here\n"
    "// note four here\n// note five here\n// note six here\n"
    "var x = 1\n"
)
GO_WRITE_EXISTING_OLD = "package p\n\n// aaa\n// bbb\n// ccc\n// ddd\n// eee\n// fff\nvar x = 1\n"
GO_WRITE_EXISTING_NEW = "package p\n\n// aaa\n// bbb\n// ccc\n// ddd\n// eee\n// fff\nvar x = 2\n"
RS_LONG_BLOCK = "/*\n one\n two\n three\n four\n five\n six\n seven\n eight\n*/\nfn f() {}\n"
RS_LONG_DOC = (
    "/// line one of the rustdoc here\n"
    "/// line two of the rustdoc here\n"
    "/// line three of the rustdoc\n"
    "/// line four of the rustdoc\n"
    "/// line five of the rustdoc\n"
    "/// line six of the rustdoc ok\n"
    "pub fn f() {}\n"
)
PY_LONG_RUN = (
    "# note one here\n# note two here\n# note three here\n# note four here\n# note five here\n# note six here\nx = 1\n"
)
PY_LONG_DOCSTRING = (
    'def f():\n    """\n'
    "    line one\n    line two\n    line three\n"
    "    line four\n    line five\n    line six\n"
    '    """\n    return 1\n'
)


def runs(matches: Iterable[Match]) -> list[list[Match]]:
    """Group comments into runs of adjacent lines — a block comment, or consecutive line
    comments with no code between — each a list of :class:`Match` in document order."""
    grouped: list[list[Match]] = []
    for m in sorted(matches, key=lambda c: c.line):
        if grouped and m.line <= grouped[-1][-1].end_line + 1:
            grouped[-1].append(m)
        else:
            grouped.append([m])
    return grouped


def too_long(run: list[Match]) -> bool:
    """Whether a comment run exceeds the line or character budget."""
    lines = run[-1].end_line - run[0].line + 1
    chars = sum(len(m.text) for m in run)
    return lines > MAX_COMMENT_LINES or chars > MAX_COMMENT_CHARS


class LongCommentIntroduced(CustomCondition):
    """True when the pending edit introduces a comment (or adjacent line-comment run) past
    the length budget. Diffs the edit's pre-image against its new text, so a comment already
    present before the edit never counts; files whose language ast-grep can't parse yield False."""

    def check(self, evt: BaseHookEvent) -> bool:
        if (
            not (file := evt.file)
            or not (lang := lang_for_path(file.path))
            or (old := evt.replaced) is None
            or (new := evt.content) is None
        ):
            return False
        return any(too_long(run) for run in runs(introduced_comments(old, new, lang)))


nudge(
    "Verbose comment introduced. Comments should be terse and used sparingly — let names, "
    "types, and organization document the code. Keep documentation-generation comments "
    "(godoc / rustdoc / docstrings) to a real description, and drop long inline commentary "
    "or anything that restates the code. See: STYLEGUIDE.md § Comments.",
    only_if=[Tool("Edit", "Write", "MultiEdit"), LongCommentIntroduced()],
    events=Event.PreToolUse,
    tests={
        # Short / within-budget comments — allowed.
        Input(file="doc.go", old=GO_NO_COMMENT, content=GO_ONE_LINE_DOC): Allow(),
        Input(file="doc.go", old=GO_NO_COMMENT, content=GO_THREE_LINE_DOC): Allow(),
        Input(file="m.py", old="x = 1\n", content="# set x\nx = 1\n"): Allow(),
        Input(file="lib.rs", old="pub fn f() {}\n", content="/// Builds a widget.\npub fn f() {}\n"): Allow(),
        # Long introduced comment runs — warned.
        Input(file="svc.go", old="package p\n\nfunc F() {\n\tx := 1\n}\n", content=GO_LONG_RUN): Warn(
            pattern="Verbose comment"
        ),  # 6-line // run > 4 lines
        Input(file="big.go", old=GO_NO_COMMENT, content="package p\n\n// " + "x" * 320 + "\nfunc F() {}\n"): Warn(
            pattern="Verbose comment"
        ),  # single line, > 300 chars
        Input(file="lib.rs", old="fn f() {}\n", content=RS_LONG_BLOCK): Warn(pattern="Verbose comment"),
        Input(file="lib.rs", old="pub fn f() {}\n", content=RS_LONG_DOC): Warn(
            pattern="Verbose comment"
        ),  # long rustdoc run also warns (strict threshold, by design)
        Input(file="m.py", old="x = 1\n", content=PY_LONG_RUN): Warn(pattern="Verbose comment"),
        # Diff-gating: a pre-existing long run re-saved unchanged does not fire.
        Input(file="svc.go", old=GO_EXISTING_OLD, content=GO_EXISTING_NEW): Allow(),
        # Python docstrings are string nodes, not comments — never trip this.
        Input(file="m.py", old="def f():\n    return 1\n", content=PY_LONG_DOCSTRING): Allow(),
        # Write tool: pre-image comes from disk at PreToolUse.
        Input(
            tool="Write",
            file=FileFixture(name="cmt_new.go", content=GO_WRITE_PLAIN),
            content=GO_WRITE_RUN,
        ): Warn(pattern="Verbose comment"),  # long run added to existing file
        Input(
            tool="Write",
            file=FileFixture(name="cmt_pre.go", content=GO_WRITE_EXISTING_OLD),
            content=GO_WRITE_EXISTING_NEW,
        ): Allow(),  # run already on disk: not introduced
    },
)
