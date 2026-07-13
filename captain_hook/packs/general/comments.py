"""Enforce terse comments: block an edit that leaves an oversized comment run, warn on the rest.

Comments should be terse and used sparingly — names, types, and organization carry the meaning.
This measures the comment blocks an edit *creates or grows* (untouched blocks stay exempt; a block's
identity is its whitespace-normalized text, so a pure move stays quiet) at their full post-edit size,
and blocks any non-doc block past 3 lines or 200 chars. Documentation-generation comments (godoc,
rustdoc, JSDoc) are carved out of the block and instead draw an advisory doc warn, and an edit whose
added lines are mostly comments draws a density warn. Language-agnostic via the tree-sitter comment
kinds in :data:`~captain_hook.ast_grep.COMMENT_TYPES`.

Design notes — accepted tradeoffs, by construction, not bugs:

* Narration directly above a Go declaration classifies as godoc: the carve-out is syntactic (next
  sibling on the following line), so a Go inline comment placed like a doc comment escapes the block.
* A new-path ``Write`` has no pre-image, so a carried-over legacy oversized comment re-trips as
  "created"; move provenance only survives an in-place edit.
* Any non-whitespace edit to a legacy oversized run re-trips it at full size — the exemption is for
  untouched and whitespace-only-reflowed runs, not for editing an oversized comment's words.
* ``yaml``/``toml``/``md``/``json`` are out of scope: only :data:`~captain_hook.types.LANG_GLOBS`
  languages parse, so comment-only formats never trip.
* Threshold boundaries are permissive (``> 3`` lines, ``> 200`` chars — the boundary value passes).
* On a deny that also carries advisories, block messages come first, then the warns.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    FileFixture,
    Input,
    Tool,
    Warn,
    hook,
    nudge,
)
from captain_hook.ast_grep import (
    comment_line_numbers,
    lang_for_path,
    touched_comment_blocks,
)

if TYPE_CHECKING:
    from captain_hook.ast_grep import CommentBlock

COMMENT_DENSITY_MIN_ADDED = 6
COMMENT_DENSITY_FRACTION = 0.5

# Fixtures for the inline tests — module constants so each physical line stays short.
GO_INBODY_LONG = (
    "package p\n\nfunc F() {\n"
    "\t// explanation line one here\n"
    "\t// explanation line two here\n"
    "\t// explanation line three here\n"
    "\t// explanation line four here\n"
    "\t// explanation line five here\n"
    "\t// explanation line six is here\n"
    "\tx := 1\n}\n"
)
GO_INBODY_201 = "package p\n\nfunc F() {\n\t// " + "x" * 198 + "\n\tx := 1\n}\n"
GO_INBODY_200 = "package p\n\nfunc F() {\n\t// " + "x" * 197 + "\n\tx := 1\n}\n"
GO_TRAILING = (
    "package p\n\nfunc F() {\n"
    "\ta := 1 // meters travelled\n"
    "\tb := 2 // meters travelled\n"
    "\tc := 3 // meters travelled\n"
    "\td := 4 // meters travelled\n}\n"
)
GO_DOC_RUN = (
    "package p\n\n"
    "// F alpha line here\n// F beta line here\n// F gamma line here\n// F delta line here\n"
    "func F() {}\n"
)
GO_CONST_DOC = (
    "package p\n\nconst (\n"
    "\t// Alpha names the first entry\n"
    "\t// with a second doc line here\n"
    "\t// and a third doc line here\n"
    "\t// and a fourth to exceed it\n"
    "\tAlpha = 1\n)\n"
)
GO_BLANK_SEP_RUN = (
    "package p\n\n"
    "// note one here\n// note two here\n// note three here\n"
    "// note four here\n// note five here\n// note six here\n"
    "\nvar x = 1\n"
)
GO_REFLOW_FILE = "package p\n\nfunc F() {\n\t/* one two three */\n\tx := 1\n}\n"
GO_REFLOW_NEW = "/*\n\t one\n\t two\n\t three\n\t*/"
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
RS_SHORT_DOC = "/// Builds a widget.\npub fn f() {}\n"
RS_EOF_DOC_200 = "/// " + "x" * 196 + "\n"
RS_EOF_DOC_201 = "/// " + "x" * 197 + "\n"
JS_POISONED = (
    "/** marker doc here */\n"
    "// narrative line one\n// narrative line two\n// narrative line three\n// narrative line four\n"
    "function f() {}\n"
)
PY_LONG_RUN = "# note one here\n# note two here\n# note three here\n# note four here\n# note five here\n# note six here\nx = 1\n"
PY_ALL_COMMENT = (
    "# note one line\n# note two line\n# note three ok\n# note four line\n# note five line\n# note six line ok\n"
)
PY_FOUR_RUN = "# one here\n# two here\n# three here\n# four here\nx = 1\n"
PY_THREE_RUN = "# one here\n# two here\n# three here\nx = 1\n"
PY_BLANK_SPLIT = "# para one aaa\n# para one bbb\n\n# para two ccc\n# para two ddd\n# para two eee\nx = 1\n"
PY_SHEBANG_HEADER = "#!/usr/bin/env python3\n# header line one\n# header line two\n# header line three\nx = 1\n"
PY_DOCSTRING = 'def f():\n    """\n    line one\n    line two\n    line three\n    line four\n    line five\n    """\n    return 1\n'
PY_GROW_OLD_FILE = "# a here\n# b here\n# c here\nx = 1\n"
PY_GROW_OLD = "# a here\n# b here\n# c here"
PY_GROW_NEW = "# a here\n# b here\n# c here\n# d here\n# e here\n# f here"
PY_NEAR_FILE = "# a here\n# b here\n# c here\n# d here\n# e here\n# f here\nx = 1\n"
PY_REFLOW_FILE = "# alpha here\n# beta here\n# gamma here\n# delta here\nx = 1\n"
PY_REFLOW_OLD = "# alpha here\n# beta here\n# gamma here\n# delta here"
PY_REFLOW_NEW = "#  alpha here\n#  beta here\n#  gamma here\n#  delta here"
PY_MULTIEDIT_NEW = "# a here\n# b here\n# c here\n# d here\n# e here\nx = 1"
PY_DENSE_FIRES = "# c1 here\na = 1\n# c2 here\nb = 2\n# c3 here\n# c4 here\nc = 3\n# c5 here\n"
PY_DENSE_HALF = "# c1 here\na = 1\n# c2 here\nb = 2\n# c3 here\nc = 3\n"
PY_DENSE_ALLOW = "# c1 here\na = 1\nb = 2\nc = 3\nd = 4\ne = 5\nf = 6\n# c2 here\n"
PY_DENSE_FLOOR = "# c1 here\n# c2 here\n# c3 here\n"
YAML_HASH = "# a\n# b\n# c\n# d\n# e\n# f\n# g\n# h\n# i\n# j\n"

BLOCK_MESSAGE = (
    "Verbose comment: this edit leaves a comment run over 3 lines / 200 chars. Comments are terse "
    "and sparing — names, types, and organization document the code. Shrink it to the one non-obvious "
    "fact, or delete it and let the code speak; long-form rationale belongs in the doc comment of the "
    "API it explains or in the commit message. See: STYLEGUIDE.md § Comments."
)
DOC_MESSAGE = (
    "Long documentation comment: doc comments (godoc / rustdoc / JSDoc) are exempt from the "
    "verbose-comment block, but keep them to a real description of the API — narrative padding and "
    "signature restatement dilute it. Tighten this one if it can say the same in fewer lines."
)
DENSITY_MESSAGE = (
    "Comment-dense edit: most of the lines this edit adds are comments. A few terse comments beat a "
    "running commentary — let names and structure carry the story, and keep only the non-obvious "
    "ones. See: STYLEGUIDE.md § Comments."
)


def touched(evt: BaseHookEvent) -> list[CommentBlock]:
    """The comment blocks this edit created or grew, or ``[]`` when the language is unparsable."""
    if (
        not (file := evt.file)
        or not (lang := lang_for_path(file.path))
        or (pre := evt.pre_image) is None
        or (post := evt.post_image) is None
    ):
        return []
    return touched_comment_blocks(pre, post, lang)


class VerboseInlineComment(CustomCondition):
    """True when the edit leaves a too-long non-doc comment block it created or grew."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(block.too_long and not block.doc for block in touched(evt))


class VerboseDocComment(CustomCondition):
    """True when the edit leaves a too-long documentation comment block it created or grew."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(block.too_long and block.doc for block in touched(evt))


class CommentDenseEdit(CustomCondition):
    """True when most of the non-blank lines this edit adds are (non-doc) comment lines — but not
    when a too-long non-doc block is present, since the verbose-comment block already covers it."""

    def check(self, evt: BaseHookEvent) -> bool:
        if (
            not (file := evt.file)
            or not (lang := lang_for_path(file.path))
            or (pre := evt.pre_image) is None
            or (post := evt.post_image) is None
        ):
            return False
        if any(block.too_long and not block.doc for block in touched_comment_blocks(pre, post, lang)):
            return False
        post_lines = post.splitlines()
        comment_lines = comment_line_numbers(post, lang, include_doc=False)
        added = comment_added = 0
        opcodes = difflib.SequenceMatcher(a=pre.splitlines(), b=post_lines, autojunk=False).get_opcodes()
        for tag, _i1, _i2, j1, j2 in opcodes:
            if tag not in ("insert", "replace"):
                continue
            for j in range(j1, j2):
                if not post_lines[j].strip():
                    continue
                added += 1
                comment_added += j + 1 in comment_lines
        return added >= COMMENT_DENSITY_MIN_ADDED and comment_added / added > COMMENT_DENSITY_FRACTION


hook(
    Event.PreToolUse,
    BLOCK_MESSAGE,
    only_if=[Tool("Edit", "Write", "MultiEdit"), VerboseInlineComment()],
    block=True,
    tests={
        # Too-long non-doc blocks an edit creates or grows — blocked.
        Input(file="svc.go", content=GO_INBODY_LONG): Block(pattern="Verbose comment"),
        Input(file="svc.go", content=GO_INBODY_201): Block(pattern="Verbose comment"),
        Input(file="lib.rs", content=RS_LONG_BLOCK): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_LONG_RUN): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_ALL_COMMENT): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_FOUR_RUN): Block(pattern="Verbose comment"),
        Input(file="var.go", content=GO_BLANK_SEP_RUN): Block(pattern="Verbose comment"),
        # Blank-line-split paragraphs merge for the size check (else they evade the block).
        Input(file="split.py", content=PY_BLANK_SPLIT): Block(pattern="Verbose comment"),
        # A marker line can't launder trailing narrative into a doc run.
        Input(file="poison.js", content=JS_POISONED): Block(pattern="Verbose comment"),
        # Growing / reflowing a run over the threshold — blocked.
        Input(file=FileFixture(name="grow.py", content=PY_GROW_OLD_FILE), old=PY_GROW_OLD, content=PY_GROW_NEW): Block(
            pattern="Verbose comment"
        ),
        Input(
            file=FileFixture(name="reflow.go", content=GO_REFLOW_FILE),
            old="/* one two three */",
            content=GO_REFLOW_NEW,
        ): Block(pattern="Verbose comment"),
        Input(
            tool="MultiEdit", file=FileFixture(name="madd.py", content="x = 1\n"), old="x = 1", content=PY_MULTIEDIT_NEW
        ): Block(pattern="Verbose comment"),
        # Boundaries, trailing comments, shebangs, and untouched / exempt blocks — allowed.
        Input(file="m.py", content=PY_THREE_RUN): Allow(),
        Input(file="ok.go", content=GO_INBODY_200): Allow(),
        Input(file="trail.go", content=GO_TRAILING): Allow(),
        Input(file="head.py", content=PY_SHEBANG_HEADER): Allow(),
        Input(file=FileFixture(name="near.py", content=PY_NEAR_FILE), old="x = 1", content="x = 2"): Allow(),
        Input(file=FileFixture(name="reflow.py", content=PY_REFLOW_FILE), old=PY_REFLOW_OLD, content=PY_REFLOW_NEW): Allow(),
        Input(file="m.py", content=PY_DOCSTRING): Allow(),
        Input(file=FileFixture(name="resave.py", content=PY_LONG_RUN), content=PY_LONG_RUN): Allow(),
        Input(file="f.yaml", content=YAML_HASH): Allow(),
        # Doc runs are carved out of the block; a density-shaped edit's short runs stay inline-clean.
        Input(file="lib.rs", content=RS_LONG_DOC): Allow(),
        Input(file="doc.go", content=GO_DOC_RUN): Allow(),
        Input(file="cd.go", content=GO_CONST_DOC): Allow(),
        Input(file="dense.py", content=PY_DENSE_FIRES): Allow(),
    },
)

nudge(
    DOC_MESSAGE,
    only_if=[Tool("Edit", "Write", "MultiEdit"), VerboseDocComment()],
    events=Event.PreToolUse,
    max_fires=None,
    tests={
        Input(file="lib.rs", content=RS_LONG_DOC): Warn(pattern="documentation comment"),
        Input(file="doc.go", content=GO_DOC_RUN): Warn(pattern="documentation comment"),
        Input(file="cd.go", content=GO_CONST_DOC): Warn(pattern="documentation comment"),
        Input(file="eof.rs", content=RS_EOF_DOC_201): Warn(pattern="documentation comment"),
        # A short doc run, a 200-char rustdoc at EOF (trailing newline not counted), and a long
        # non-doc run all leave the doc warn quiet.
        Input(file="lib.rs", content=RS_SHORT_DOC): Allow(),
        Input(file="eof.rs", content=RS_EOF_DOC_200): Allow(),
        Input(file="m.py", content=PY_LONG_RUN): Allow(),
    },
)

nudge(
    DENSITY_MESSAGE,
    only_if=[Tool("Edit", "Write", "MultiEdit"), CommentDenseEdit()],
    events=Event.PreToolUse,
    max_fires=None,
    tests={
        Input(file="dense.py", content=PY_DENSE_FIRES): Warn(pattern="Comment-dense"),
        # The block already covers an all-comment edit; the density warn stands down.
        Input(file="all.py", content=PY_ALL_COMMENT): Allow(),
        # Exactly 50% is not "most": no warn.
        Input(file="half.py", content=PY_DENSE_HALF): Allow(),
        Input(file="sparse.py", content=PY_DENSE_ALLOW): Allow(),
        Input(file="floor.py", content=PY_DENSE_FLOOR): Allow(),
    },
)
