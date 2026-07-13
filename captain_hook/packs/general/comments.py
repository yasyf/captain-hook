"""Enforce terse comments: block an edit that leaves an oversized comment run, warn on the rest.

Comments should be terse and used sparingly — names, types, and organization carry the meaning.
This measures the comment runs an edit *creates or grows* (untouched runs stay exempt; a run's
identity is its whitespace-normalized text, so a reflow never counts) at their full post-edit size,
and blocks any non-doc run past 3 lines or 200 chars. Documentation-generation comments (godoc,
rustdoc, JSDoc) are carved out of the block and instead draw an advisory doc warn, and an edit whose
added lines are mostly comments draws a density warn. Language-agnostic via the tree-sitter comment
kinds in :data:`~captain_hook.ast_grep.COMMENT_TYPES`.
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
    touched_comment_runs,
)

if TYPE_CHECKING:
    from captain_hook.ast_grep import CommentRun

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
GO_DOC_RUN = (
    "package p\n\n"
    "// F alpha line here\n// F beta line here\n// F gamma line here\n// F delta line here\n"
    "func F() {}\n"
)
GO_BLANK_SEP_RUN = (
    "package p\n\n"
    "// note one here\n// note two here\n// note three here\n"
    "// note four here\n// note five here\n// note six here\n"
    "\nvar x = 1\n"
)
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
PY_LONG_RUN = "# note one here\n# note two here\n# note three here\n# note four here\n# note five here\n# note six here\nx = 1\n"
PY_FOUR_RUN = "# one here\n# two here\n# three here\n# four here\nx = 1\n"
PY_THREE_RUN = "# one here\n# two here\n# three here\nx = 1\n"
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


def touched(evt: BaseHookEvent) -> list[CommentRun]:
    """The comment runs this edit created or grew, or ``[]`` when the language is unparsable."""
    if (
        not (file := evt.file)
        or not (lang := lang_for_path(file.path))
        or (pre := evt.pre_image) is None
        or (post := evt.post_image) is None
    ):
        return []
    return touched_comment_runs(pre, post, lang)


class VerboseInlineComment(CustomCondition):
    """True when the edit leaves a too-long non-doc comment run it created or grew."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(run.too_long and not run.doc for run in touched(evt))


class VerboseDocComment(CustomCondition):
    """True when the edit leaves a too-long documentation comment run it created or grew."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(run.too_long and run.doc for run in touched(evt))


class CommentDenseEdit(CustomCondition):
    """True when most of the non-blank lines this edit adds are (non-doc) comment lines."""

    def check(self, evt: BaseHookEvent) -> bool:
        if (
            not (file := evt.file)
            or not (lang := lang_for_path(file.path))
            or (pre := evt.pre_image) is None
            or (post := evt.post_image) is None
        ):
            return False
        post_lines = post.splitlines()
        comment_lines = comment_line_numbers(post, lang, include_doc=False)
        added = comment_added = 0
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=pre.splitlines(), b=post_lines, autojunk=False).get_opcodes():
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
        # Too-long non-doc runs an edit creates or grows — blocked.
        Input(file="svc.go", content=GO_INBODY_LONG): Block(pattern="Verbose comment"),
        Input(file="svc.go", content=GO_INBODY_201): Block(pattern="Verbose comment"),
        Input(file="lib.rs", content=RS_LONG_BLOCK): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_LONG_RUN): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_FOUR_RUN): Block(pattern="Verbose comment"),
        Input(file="var.go", content=GO_BLANK_SEP_RUN): Block(pattern="Verbose comment"),
        Input(file=FileFixture(name="grow.py", content=PY_GROW_OLD_FILE), old=PY_GROW_OLD, content=PY_GROW_NEW): Block(
            pattern="Verbose comment"
        ),
        Input(
            tool="MultiEdit", file=FileFixture(name="madd.py", content="x = 1\n"), old="x = 1", content=PY_MULTIEDIT_NEW
        ): Block(pattern="Verbose comment"),
        # Boundaries and untouched / exempt runs — allowed.
        Input(file="m.py", content=PY_THREE_RUN): Allow(),
        Input(file="ok.go", content=GO_INBODY_200): Allow(),
        Input(file=FileFixture(name="near.py", content=PY_NEAR_FILE), old="x = 1", content="x = 2"): Allow(),
        Input(file=FileFixture(name="reflow.py", content=PY_REFLOW_FILE), old=PY_REFLOW_OLD, content=PY_REFLOW_NEW): Allow(),
        Input(file="m.py", content=PY_DOCSTRING): Allow(),
        Input(file=FileFixture(name="resave.py", content=PY_LONG_RUN), content=PY_LONG_RUN): Allow(),
        Input(file="f.yaml", content=YAML_HASH): Allow(),
        # Doc runs are carved out of the block; a density-shaped edit's short runs stay inline-clean.
        Input(file="lib.rs", content=RS_LONG_DOC): Allow(),
        Input(file="doc.go", content=GO_DOC_RUN): Allow(),
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
        # A short doc run and a long non-doc run both leave the doc warn quiet.
        Input(file="lib.rs", content=RS_SHORT_DOC): Allow(),
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
        Input(file="sparse.py", content=PY_DENSE_ALLOW): Allow(),
        Input(file="floor.py", content=PY_DENSE_FLOOR): Allow(),
    },
)
