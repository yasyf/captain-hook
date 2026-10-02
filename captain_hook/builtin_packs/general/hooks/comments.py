"""Block an edit that leaves an over-budget comment block it created or lengthened; warn on legacy rewords, doc,
and dense cases."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from cc_transcript.tools import SpanEditCall

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
    MAX_COMMENT_CHARS,
    MAX_COMMENT_LINES,
    comment_line_numbers,
    lang_for_path,
    touched_comment_ancestry,
    touched_comment_blocks,
)

if TYPE_CHECKING:
    from captain_hook.ast_grep import CommentBlock, TouchedComment

DOC_HEAD_LINES = 6
DOC_HEAD_CHARS = 400

GO_DOC_RUN = (
    "package p\n\n// F alpha line here\n// F beta line here\n// F gamma line here\n// F delta line here\nfunc F() {}\n"
)
GO_CONST_DOC = (
    "package p\n\nconst (\n"
    "\t// Alpha names the first entry\n"
    "\t// with a second doc line here\n"
    "\t// and a third doc line here\n"
    "\t// and a fourth to exceed it\n"
    "\tAlpha = 1\n)\n"
)
GO_TAIL_DOC = (
    "package p\n\n"
    "// PeerAuditToken mints the audit token\n"
    "// for a peer exchange and binds it\n"
    "// to the session nonce.\n"
    "//\n"
    "// The token is minted before the check\n"
    "// deliberately: the mint-verify TOCTOU\n"
    "// window is closed by binding the nonce\n"
    "// so a swapped peer cannot replay a\n"
    "// stale token from an earlier session.\n"
    "func PeerAuditToken() {}\n"
)
GO_SEVEN_DOC = (
    "package p\n\n"
    "// F line one describes the function\n"
    "// F line two describes the function\n"
    "// F line three describes the function\n"
    "// F line four describes the function\n"
    "// F line five describes the function\n"
    "// F line six describes the function\n"
    "// F line seven describes the function\n"
    "func F() {}\n"
)
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
PY_ALL_COMMENT = (
    "# note one line\n# note two line\n# note three ok\n# note four line\n# note five line\n# note six line ok\n"
)
GO_LEGACY_RUN = (
    "package p\n\nfunc F() {\n"
    "\t// legacy note that runs long enough to push three lines past the char budget one\n"
    "\t// legacy note that runs long enough to push three lines past the char budget two\n"
    "\t// legacy note that runs long enough to push three lines past the char budget six\n"
    "\tx := 1\n}\n"
)
PY_DENSE_FIRES = "# c1 here\na = 1\n# c2 here\nb = 2\n# c3 here\n# c4 here\nc = 3\n# c5 here\n"


def touched_ancestry(evt: BaseHookEvent) -> list[TouchedComment]:
    """The comment blocks this edit created or grew, each paired with its pre-edit over-budget ancestor,
    or ``[]`` when the language is unparsable."""
    if not (file := evt.file) or not (lang := lang_for_path(file.path)):
        return []
    if (post := evt.post_image) is None:
        # WORKAROUND: span edits carry no post-image, so diff the whole-file pre-image against the new span text.
        if not isinstance(evt.input, SpanEditCall) or (pre := evt.replaced) is None or (post := evt.content) is None:
            return []
    elif (pre := evt.pre_image) is None:
        return []
    return touched_comment_ancestry(pre, post, lang)


def lengthened(touched: TouchedComment) -> bool:
    """True when the block is new, or has more lines than its over-budget ancestor."""
    return touched.ancestor is None or touched.block.lines > touched.ancestor.lines


def touched(evt: BaseHookEvent) -> list[CommentBlock]:
    """The comment blocks this edit created or grew, or ``[]`` when the language is unparsable."""
    return [t.block for t in touched_ancestry(evt)]


class VerboseComment(CustomCondition):
    """True when the edit leaves an over-budget comment block it created, or added lines to."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(t.block.too_long and lengthened(t) for t in touched_ancestry(evt))


class LegacyCommentEdit(CustomCondition):
    """True when the edit rewords a comment that was already over budget without adding lines."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(t.block.too_long and not lengthened(t) for t in touched_ancestry(evt))


class VerboseDocComment(CustomCondition):
    """True when an allowed doc block has an opening paragraph over the plain budget."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            block.doc and not block.too_long and block.doc_paragraphs[0].over(MAX_COMMENT_LINES, MAX_COMMENT_CHARS)
            for block in touched(evt)
        )


class CommentDenseEdit(CustomCondition):
    """True when most of the non-blank lines this edit adds are (non-doc) comment lines — but not
    when the verbose-comment block already fires on this edit."""

    def check(self, evt: BaseHookEvent) -> bool:
        if (
            not (file := evt.file)
            or not (lang := lang_for_path(file.path))
            or (pre := evt.pre_image) is None
            or (post := evt.post_image) is None
        ):
            return False
        if any(block.too_long for block in touched_comment_blocks(pre, post, lang)):
            return False
        post_lines = post.splitlines()
        comment_lines = comment_line_numbers(post, lang, include_doc=False)
        opcodes = difflib.SequenceMatcher(a=pre.splitlines(), b=post_lines, autojunk=False).get_opcodes()
        flags = [
            j + 1 in comment_lines
            for tag, _i1, _i2, j1, j2 in opcodes
            if tag in ("insert", "replace")
            for j in range(j1, j2)
            if post_lines[j].strip()
        ]
        added = len(flags)
        comment_added = sum(flags)
        return added >= 6 and comment_added / added > 0.5


hook(
    Event.PreToolUse,
    (
        f"Verbose comment: a plain comment run is capped at {MAX_COMMENT_LINES} lines / {MAX_COMMENT_CHARS} chars, "
        f"and a doc comment's opening paragraph at {DOC_HEAD_LINES} lines / {DOC_HEAD_CHARS} chars. "
        "Shrink it to the one non-obvious fact or delete it."
    ),
    only_if=[Tool("Edit", "Write", "MultiEdit"), VerboseComment()],
    block=True,
    tests={
        Input(
            file="svc.go",
            content=(
                "package p\n\nfunc F() {\n"
                "\t// explanation line one here\n"
                "\t// explanation line two here\n"
                "\t// explanation line three here\n"
                "\t// explanation line four here\n"
                "\t// explanation line five here\n"
                "\t// explanation line six is here\n"
                "\tx := 1\n}\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(file="svc.go", content="package p\n\nfunc F() {\n\t// " + "x" * 198 + "\n\tx := 1\n}\n"): Block(
            pattern="Verbose comment"
        ),
        Input(
            file="lib.rs",
            content="/*\n one\n two\n three\n four\n five\n six\n seven\n eight\n*/\nfn f() {}\n",
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(name="peer.go", content="package p\n\nfunc PeerAuditToken() {}\n"),
            old="func PeerAuditToken() {}",
            content=GO_TAIL_DOC.removeprefix("package p\n\n").rstrip(),
        ): Block(pattern="Verbose comment"),
        Input(file="seven.go", content=GO_SEVEN_DOC): Block(pattern="Verbose comment"),
        Input(file="chars.go", content="package p\n\n// " + "x" * 398 + "\nfunc F() {}\n"): Block(
            pattern="Verbose comment"
        ),
        Input(
            file="tail.rs",
            content=(
                "/// Summary one here\n"
                "/// Summary two here\n"
                "///\n"
                "/// tail line one here\n"
                "/// tail line two here\n"
                "/// tail line three here\n"
                "/// tail line four here\n"
                "pub fn f() {}\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(
            file="tail.js",
            content=(
                "/**\n"
                " * Summary here.\n"
                " *\n"
                " * tail line one here\n"
                " * tail line two here\n"
                " * tail line three here\n"
                " * tail line four here\n"
                " */\n"
                "function f() {}\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(
            file="tail.swift",
            content=(
                "/// Summary one here\n"
                "/// Summary two here\n"
                "///\n"
                "/// tail line one here\n"
                "/// tail line two here\n"
                "/// tail line three here\n"
                "/// tail line four here\n"
                "func f() {}\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_LONG_RUN): Block(pattern="Verbose comment"),
        Input(file="m.py", content=PY_ALL_COMMENT): Block(pattern="Verbose comment"),
        Input(file="m.py", content="# one here\n# two here\n# three here\n# four here\nx = 1\n"): Block(
            pattern="Verbose comment"
        ),
        Input(
            file="var.go",
            content=(
                "package p\n\n"
                "// note one here\n// note two here\n// note three here\n"
                "// note four here\n// note five here\n// note six here\n"
                "\nvar x = 1\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(
            file="split.py",
            content="# para one aaa\n# para one bbb\n\n# para two ccc\n# para two ddd\n# para two eee\nx = 1\n",
        ): Block(pattern="Verbose comment"),
        Input(
            file="poison.js",
            content=(
                "/** marker doc here */\n"
                "// narrative line one\n"
                "// narrative line two\n"
                "// narrative line three\n"
                "// narrative line four\n"
                "// narrative line five\n"
                "// narrative line six\n"
                "function f() {}\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(
            file="floating.js",
            content=(
                "/**\n"
                " * floating line one here\n"
                " * floating line two here\n"
                " * floating line three here\n"
                " * floating line four here\n"
                " */\n"
                "\n"
                "x();\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(name="grow.py", content="# a here\n# b here\n# c here\nx = 1\n"),
            old="# a here\n# b here\n# c here",
            content="# a here\n# b here\n# c here\n# d here\n# e here\n# f here",
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(name="legacy-grow.py", content=PY_LONG_RUN),
            old="# note six here",
            content="# note six here\n# note seven here",
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(name="legacy-grow.go", content=GO_LEGACY_RUN),
            old="\tx := 1",
            content="\t// and one more line\n\tx := 1",
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(name="reflow.go", content="package p\n\nfunc F() {\n\t/* one two three */\n\tx := 1\n}\n"),
            old="/* one two three */",
            content="/*\n\t one\n\t two\n\t three\n\t*/",
        ): Block(pattern="Verbose comment"),
        Input(
            tool="MultiEdit",
            file=FileFixture(name="madd.py", content="x = 1\n"),
            old="x = 1",
            content="# a here\n# b here\n# c here\n# d here\n# e here\nx = 1",
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(
                name="relocate.py",
                content="# gone one here\n# gone two here\n# gone three here\n# gone four here\nx = 1\ny = 2\n",
            ),
            old="# gone one here\n# gone two here\n# gone three here\n# gone four here\nx = 1\ny = 2",
            content="x = 1\n# new one here\n# new two here\n# new three here\n# new four here\ny = 2",
        ): Block(pattern="Verbose comment"),
        Input(file="m.py", content="# one here\n# two here\n# three here\nx = 1\n"): Allow(),
        Input(file="ok.go", content="package p\n\nfunc F() {\n\t// " + "x" * 197 + "\n\tx := 1\n}\n"): Allow(),
        Input(file="chars.go", content="package p\n\n// " + "x" * 397 + "\nfunc F() {}\n"): Allow(),
        Input(
            file="trail.go",
            content=(
                "package p\n\nfunc F() {\n"
                "\ta := 1 // meters travelled\n"
                "\tb := 2 // meters travelled\n"
                "\tc := 3 // meters travelled\n"
                "\td := 4 // meters travelled\n}\n"
            ),
        ): Allow(),
        Input(
            file="head.py",
            content="#!/usr/bin/env python3\n# header line one\n# header line two\n# header line three\nx = 1\n",
        ): Allow(),
        Input(
            file="tool.py",
            content=(
                "#!/usr/bin/env -S uv run --script\n"
                "# /// script\n"
                '# requires-python = ">=3.13"\n'
                '# dependencies = ["fleetlib"]\n'
                "#\n"
                "# [tool.uv.sources]\n"
                '# fleetlib = { path = "fleetlib", editable = true }\n'
                "# ///\n"
                '"""Tool."""\n\nx = 1\n'
            ),
        ): Allow(),
        Input(
            file="fake.py",
            content=(
                "# /// script\n"
                "# narrative line one here\n"
                "# narrative line two here\n"
                "# narrative line three here\n"
                "# ///\n"
                "x = 1\n"
            ),
        ): Block(pattern="Verbose comment"),
        Input(
            file=FileFixture(
                name="near.py", content="# a here\n# b here\n# c here\n# d here\n# e here\n# f here\nx = 1\n"
            ),
            old="x = 1",
            content="x = 2",
        ): Allow(),
        Input(
            file=FileFixture(
                name="reflow.py", content="# alpha here\n# beta here\n# gamma here\n# delta here\nx = 1\n"
            ),
            old="# alpha here\n# beta here\n# gamma here\n# delta here",
            content="#  alpha here\n#  beta here\n#  gamma here\n#  delta here",
        ): Allow(),
        Input(
            file="m.py",
            content=(
                'def f():\n    """\n    line one\n    line two\n    line three\n'
                '    line four\n    line five\n    """\n    return 1\n'
            ),
        ): Allow(),
        Input(file=FileFixture(name="resave.py", content=PY_LONG_RUN), content=PY_LONG_RUN): Allow(),
        Input(
            file=FileFixture(name="keep.go", content=GO_TAIL_DOC),
            old="func PeerAuditToken() {}",
            content="func PeerAuditToken() { _ = 1 }",
        ): Allow(),
        Input(
            file=FileFixture(name="space.go", content=GO_TAIL_DOC),
            old="// deliberately: the mint-verify TOCTOU",
            content="//  deliberately: the mint-verify TOCTOU",
        ): Allow(),
        Input(
            file=FileFixture(name="legacy-light.py", content=PY_LONG_RUN),
            old="# note two here",
            content="# note two reworded here",
        ): Allow(),
        Input(
            file=FileFixture(name="legacy-rewrite.py", content=PY_LONG_RUN),
            old=(
                "# note one here\n# note two here\n# note three here\n"
                "# note four here\n# note five here\n# note six here"
            ),
            content=(
                "# fully rewritten line a\n# fully rewritten line b\n# fully rewritten line c\n"
                "# fully rewritten line d\n# fully rewritten line e\n# fully rewritten line f"
            ),
        ): Allow(),
        Input(tool="Write", file="write.go", content=GO_TAIL_DOC): Block(pattern="Verbose comment"),
        Input(file="f.yaml", content="# a\n# b\n# c\n# d\n# e\n# f\n# g\n# h\n# i\n# j\n"): Block(
            pattern="Verbose comment"
        ),
        Input(file="lib.rs", content=RS_LONG_DOC): Allow(),
        Input(file="doc.go", content=GO_DOC_RUN): Allow(),
        Input(file="cd.go", content=GO_CONST_DOC): Allow(),
        Input(
            file="plain.rs",
            content="// line one here\n// line two here\n// line three here\n// line four here\nfn f() {}\n",
        ): Allow(),
        Input(file="dense.py", content=PY_DENSE_FIRES): Allow(),
    },
)

nudge(
    (
        "Legacy long comment: this edit reworks a comment that was already over budget. "
        "Trim it to the one non-obvious fact or delete it."
    ),
    only_if=[Tool("Edit", "Write", "MultiEdit"), LegacyCommentEdit()],
    events=Event.PreToolUse,
    max_fires=None,
    advisory_on_deny=True,
    tests={
        Input(
            file=FileFixture(name="legacy-light.py", content=PY_LONG_RUN),
            old="# note two here",
            content="# note two reworded here",
        ): Warn(pattern="Legacy long comment"),
        Input(
            file=FileFixture(name="legacy-rewrite.py", content=PY_LONG_RUN),
            old=(
                "# note one here\n# note two here\n# note three here\n"
                "# note four here\n# note five here\n# note six here"
            ),
            content=(
                "# fully rewritten line a\n# fully rewritten line b\n# fully rewritten line c\n"
                "# fully rewritten line d\n# fully rewritten line e\n# fully rewritten line f"
            ),
        ): Warn(pattern="Legacy long comment"),
        Input(
            file=FileFixture(name="legacy-tail.go", content=GO_TAIL_DOC),
            old="// stale token from an earlier session.",
            content="// stale token from a much earlier session.",
        ): Warn(pattern="Legacy long comment"),
        Input(file="new.py", content=PY_LONG_RUN): Allow(),
        Input(
            file=FileFixture(name="legacy-grow.py", content=PY_LONG_RUN),
            old="# note six here",
            content="# note six here\n# note seven here",
        ): Allow(),
        Input(
            file=FileFixture(name="grow.py", content="# a here\n# b here\n# c here\nx = 1\n"),
            old="# a here\n# b here\n# c here",
            content="# a here\n# b here\n# c here\n# d here\n# e here\n# f here",
        ): Allow(),
        Input(
            file=FileFixture(name="trim.py", content=PY_LONG_RUN),
            old=(
                "# note one here\n# note two here\n# note three here\n"
                "# note four here\n# note five here\n# note six here"
            ),
            content="# just one terse note",
        ): Allow(),
        Input(file=FileFixture(name="resave.py", content=PY_LONG_RUN), content=PY_LONG_RUN): Allow(),
        Input(
            file=FileFixture(
                name="reflow.py", content="# alpha here\n# beta here\n# gamma here\n# delta here\nx = 1\n"
            ),
            old="# alpha here\n# beta here\n# gamma here\n# delta here",
            content="#  alpha here\n#  beta here\n#  gamma here\n#  delta here",
        ): Allow(),
        Input(tool="Write", file="write.go", content=GO_TAIL_DOC): Allow(),
    },
)

nudge(
    (
        f"Long documentation comment: the opening paragraph runs past {MAX_COMMENT_LINES} lines / "
        f"{MAX_COMMENT_CHARS} chars. "
        "Cut it to a real description of the API in fewer lines."
    ),
    only_if=[Tool("Edit", "Write", "MultiEdit"), VerboseDocComment()],
    events=Event.PreToolUse,
    max_fires=None,
    advisory_on_deny=True,
    tests={
        Input(file="lib.rs", content=RS_LONG_DOC): Warn(pattern="documentation comment"),
        Input(file="doc.go", content=GO_DOC_RUN): Warn(pattern="documentation comment"),
        Input(file="cd.go", content=GO_CONST_DOC): Warn(pattern="documentation comment"),
        Input(file="eof.rs", content="/// " + "x" * 197 + "\n"): Warn(pattern="documentation comment"),
        Input(
            file="plain.rs",
            content="// line one here\n// line two here\n// line three here\n// line four here\nfn f() {}\n",
        ): Warn(pattern="documentation comment"),
        Input(file="lib.rs", content="/// Builds a widget.\npub fn f() {}\n"): Allow(),
        Input(file="eof.rs", content="/// " + "x" * 196 + "\n"): Allow(),
        Input(file="m.py", content=PY_LONG_RUN): Allow(),
        Input(file="tail.go", content=GO_TAIL_DOC): Allow(),
        Input(file="seven.go", content=GO_SEVEN_DOC): Allow(),
        Input(
            file="mid-tail.go",
            content=(
                "package p\n\n"
                "// F head line one here\n"
                "// F head line two here\n"
                "// F head line three here\n"
                "// F head line four here\n"
                "//\n"
                "// tail line one here\n"
                "// tail line two here\n"
                "// tail line three here\n"
                "// tail line four here\n"
                "func F() {}\n"
            ),
        ): Allow(),
    },
)

nudge(
    (
        "Comment-dense edit: most lines this edit adds are comments. "
        "Delete every comment the names and structure already carry."
    ),
    only_if=[Tool("Edit", "Write", "MultiEdit"), CommentDenseEdit()],
    events=Event.PreToolUse,
    max_fires=None,
    advisory_on_deny=True,
    tests={
        Input(file="dense.py", content=PY_DENSE_FIRES): Warn(pattern="Comment-dense"),
        Input(file="all.py", content=PY_ALL_COMMENT): Allow(),
        Input(
            file="fence.py",
            content=(
                "# /// script\n"
                '# requires-python = ">=3.13"\n'
                '# dependencies = ["a", "b"]\n'
                "#\n"
                "# [tool.uv.sources]\n"
                '# a = { path = "a" }\n'
                "# ///\n"
                "x = 1\n"
            ),
        ): Allow(),
        Input(
            file=FileFixture(
                name="doc-dense.rs",
                content=(
                    "pub fn f() {\n"
                    "    let a = 1;\n"
                    "    let b = 2;\n"
                    "    let c = 3;\n"
                    "    let d = 4;\n"
                    "    let e = 5;\n"
                    "    let f = 6;\n"
                    "    let g = 7;\n"
                    "    let h = 8;\n"
                    "}\n"
                ),
            ),
            old=(
                "pub fn f() {\n"
                "    let a = 1;\n"
                "    let b = 2;\n"
                "    let c = 3;\n"
                "    let d = 4;\n"
                "    let e = 5;\n"
                "    let f = 6;\n"
                "    let g = 7;\n"
                "    let h = 8;\n"
                "}"
            ),
            content=(
                "/// doc line one here\n"
                "/// doc line two here\n"
                "/// doc line three here\n"
                "/// doc line four here\n"
                "/// doc line five here\n"
                "/// doc line six here\n"
                "/// doc line seven here\n"
                "pub fn f() {\n"
                "    // note a here\n"
                "    let a = 1;\n"
                "    // note b here\n"
                "    let b = 2;\n"
                "    // note c here\n"
                "    let c = 3;\n"
                "    // note d here\n"
                "    let d = 4;\n"
                "    // note e here\n"
                "    let e = 5;\n"
                "    // note f here\n"
                "    let f = 6;\n"
                "    // note g here\n"
                "    let g = 7;\n"
                "    // note h here\n"
                "    let h = 8;\n"
                "}"
            ),
        ): Allow(),
        Input(file="half.py", content="# c1 here\na = 1\n# c2 here\nb = 2\n# c3 here\nc = 3\n"): Allow(),
        Input(file="sparse.py", content="# c1 here\na = 1\nb = 2\nc = 3\nd = 4\ne = 5\nf = 6\n# c2 here\n"): Allow(),
        Input(file="floor.py", content="# c1 here\n# c2 here\n# c3 here\n"): Allow(),
    },
)
