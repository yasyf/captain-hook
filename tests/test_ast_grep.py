from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.ast_grep import (
    COMMENT_TYPES,
    Match,
    SyntaxNode,
    comment_blocks,
    comment_line_numbers,
    comment_runs,
    comments,
    find_introduced,
    find_kinds,
    introduced_comments,
    lang_for_path,
    parse,
    touched_comment_blocks,
)


class TestSyntaxNode:
    def test_parse_returns_wrapper(self) -> None:
        root = parse("x = 1\n", "py")
        assert isinstance(root, SyntaxNode)
        assert root.kind == "module"
        assert root.text == "x = 1\n"

    def test_descendants_document_order(self) -> None:
        kinds = [n.kind for n in parse("x = 1\n# note\n", "py").descendants()]
        assert kinds == ["expression_statement", "assignment", "identifier", "=", "integer", "comment"]

    def test_to_match_is_one_based(self) -> None:
        node = next(n for n in parse("x = 1\n# note\n", "py").descendants() if n.kind == "comment")
        assert node.to_match() == Match(line=2, end_line=2, text="# note")


class TestFindKinds:
    def test_membership_walk(self) -> None:
        assert list(find_kinds("x = 1\ny = 2\n", "py", {"integer"})) == [
            Match(line=1, end_line=1, text="1"),
            Match(line=2, end_line=2, text="2"),
        ]

    def test_kind_the_grammar_lacks_yields_nothing(self) -> None:
        # The ast-grep kind matcher raises on "line_comment" for py; the walk just misses.
        assert list(find_kinds("# hi\n", "py", {"line_comment"})) == []


class TestComments:
    def test_comment_types_union(self) -> None:
        assert COMMENT_TYPES == frozenset({"comment", "line_comment", "block_comment"})

    @pytest.mark.parametrize(
        ("source", "lang", "expected"),
        [
            pytest.param(
                "# top\nx = 1  # trailing\n",
                "py",
                [Match(line=1, end_line=1, text="# top"), Match(line=2, end_line=2, text="# trailing")],
                id="py_hash_comments",
            ),
            pytest.param(
                "// line\n/* block\nspans lines */\nlet x = 1;\n",
                "js",
                [
                    Match(line=1, end_line=1, text="// line"),
                    Match(line=2, end_line=3, text="/* block\nspans lines */"),
                ],
                id="js_line_and_block",
            ),
            pytest.param(
                "// ts line\nconst x: number = 1; /* ts block */\n",
                "ts",
                [Match(line=1, end_line=1, text="// ts line"), Match(line=2, end_line=2, text="/* ts block */")],
                id="ts_line_and_block",
            ),
            pytest.param(
                "/// doc comment\nfn main() {\n    // plain line\n    /* block */\n}\n",
                "rs",
                [
                    Match(line=1, end_line=2, text="/// doc comment\n"),
                    Match(line=3, end_line=3, text="// plain line"),
                    Match(line=4, end_line=4, text="/* block */"),
                ],
                id="rs_doc_comment_extracts_once_at_top_level",
            ),
            pytest.param(
                "package main\n// go line\n/* go block */\n",
                "go",
                [Match(line=2, end_line=2, text="// go line"), Match(line=3, end_line=3, text="/* go block */")],
                id="go_line_and_block",
            ),
            pytest.param(
                "#!/bin/sh\n# a comment\necho hi\n",
                "bash",
                [Match(line=1, end_line=1, text="#!/bin/sh"), Match(line=2, end_line=2, text="# a comment")],
                id="bash_shebang_is_a_comment_node",
            ),
            pytest.param(
                "const el = <div>{/* jsx block */}</div>;\n",
                "tsx",
                [Match(line=1, end_line=1, text="/* jsx block */")],
                id="tsx_expression_block",
            ),
            pytest.param(
                "const el = <div>{/* jsx block */}</div>;\n",
                "jsx",
                [Match(line=1, end_line=1, text="/* jsx block */")],
                id="jsx_expression_block",
            ),
            pytest.param(
                "// j line\n/* j block */\nclass A {}\n",
                "java",
                [Match(line=1, end_line=1, text="// j line"), Match(line=2, end_line=2, text="/* j block */")],
                id="java_line_and_block_comment_kinds",
            ),
        ],
    )
    def test_comments_across_languages(self, source: str, lang: str, expected: list[Match]) -> None:
        assert list(comments(source, lang)) == expected

    def test_multiline_block_is_one_match(self) -> None:
        [match] = comments("/* one\ntwo\nthree */\nx();\n", "js")
        assert match == Match(line=1, end_line=3, text="/* one\ntwo\nthree */")
        assert match.text.count("\n") == 2


class TestIntroducedComments:
    def test_preexisting_comment_not_rereported(self) -> None:
        old = "# removed the retry logic\nx()\n"
        new = "# removed the retry logic\ny()\n"
        assert list(introduced_comments(old, new, "py")) == []

    def test_moved_and_respaced_comment_not_rereported(self) -> None:
        old = "# keep me\nx = 1\n"
        new = "x = 1\ny = 2\n#  keep   me\n"
        assert list(introduced_comments(old, new, "py")) == []

    def test_new_file_reports_all(self) -> None:
        assert list(introduced_comments("", "# one\nx = 1\n# two\n", "py")) == [
            Match(line=1, end_line=1, text="# one"),
            Match(line=3, end_line=3, text="# two"),
        ]

    def test_new_comment_reported_with_location(self) -> None:
        assert list(introduced_comments("x = 1\n", "x = 1\n# no longer needed\n", "py")) == [
            Match(line=2, end_line=2, text="# no longer needed")
        ]


class TestFindIntroduced:
    def test_pattern_diff_still_works(self) -> None:
        assert list(find_introduced("print(1)\n", "print(1)\nprint(2)\n", "py", "print($$$)")) == [
            Match(line=2, end_line=2, text="print(2)")
        ]

    def test_preexisting_pattern_not_reported(self) -> None:
        assert list(find_introduced("print(1)\n", "x = 2\nprint(1)\n", "py", "print($$$)")) == []


class TestCommentRuns:
    def test_adjacent_comments_group_blank_line_splits(self) -> None:
        runs = comment_runs("# a\n# b\n\n# c\nx = 1\n", "py")
        assert [(r.line, r.end_line, r.texts) for r in runs] == [
            (1, 2, ("# a", "# b")),
            (4, 4, ("# c",)),
        ]

    def test_trailing_comments_are_singletons(self) -> None:
        # A trailing comment (code before it) never groups with a neighbour.
        runs = comment_runs("x = 1  # trailing\n# leading\n", "py")
        assert [(r.texts, r.leading) for r in runs] == [(("# trailing",), False), (("# leading",), True)]

    def test_shebang_excluded(self) -> None:
        runs = comment_runs("#!/bin/sh\n# real one\n# real two\n", "bash")
        assert [r.texts for r in runs] == [("# real one", "# real two")]

    def test_run_size_measures(self) -> None:
        [run] = comment_runs("// " + "z" * 40 + "\nx();\n", "js")
        assert run.lines == 1
        assert run.chars == 43
        assert run.key == "// " + "z" * 40

    def test_chars_drop_trailing_newline(self) -> None:
        # A rust doc line at EOF folds the newline into its node text; chars must not count it.
        [run] = comment_runs("/// " + "x" * 196 + "\n", "rs")
        assert run.chars == 200

    def test_key_normalizes_whitespace(self) -> None:
        [a] = comment_runs("#  spaced   out\nx = 1\n", "py")
        [b] = comment_runs("# spaced out\nx = 1\n", "py")
        assert a.key == b.key == "# spaced out"

    @pytest.mark.parametrize(
        ("source", "lang", "doc"),
        [
            pytest.param("/// docline\n/// more\npub fn f() {}\n", "rs", True, id="rs_triple_slash_doc"),
            pytest.param("//! inner doc\nmod m {}\n", "rs", True, id="rs_bang_doc"),
            pytest.param("/* plain */\nfn f() {}\n", "rs", False, id="rs_plain_block_inline"),
            pytest.param(
                "/** m */\n// narrative\nfunction f() {}\n", "js", False, id="js_marker_then_narrative_poisoned"
            ),
            pytest.param("package p\n\n// F does it.\nfunc F() {}\n", "go", True, id="go_adjacent_is_doc"),
            pytest.param("package p\n\nconst (\n\t// doc\n\tX = 1\n)\n", "go", True, id="go_grouped_const_spec_doc"),
            pytest.param("package p\n\ntype T struct {\n\t// doc\n\tF int\n}\n", "go", True, id="go_struct_field_doc"),
            pytest.param("package p\n\n// note\n\nfunc F() {}\n", "go", False, id="go_blank_separated_inline"),
            pytest.param("package p\n\nfunc F() {\n\t// x\n\ty := 1\n}\n", "go", False, id="go_in_body_inline"),
            pytest.param("/**\n * hi\n */\nfunction f() {}\n", "js", True, id="js_jsdoc"),
            pytest.param("# just a comment\nx = 1\n", "py", False, id="py_never_doc"),
        ],
    )
    def test_doc_classification(self, source: str, lang: str, doc: bool) -> None:
        assert comment_runs(source, lang)[0].doc is doc

    def test_comment_line_numbers_excludes_doc(self) -> None:
        source = "/// doc line\n/// doc two\npub fn f() {\n    // inline\n    let x = 1;\n}\n"
        assert comment_line_numbers(source, "rs", include_doc=False) == {4}
        assert 1 in comment_line_numbers(source, "rs", include_doc=True)


class TestCommentBlocks:
    def test_blank_split_paragraphs_merge(self) -> None:
        [block] = comment_blocks("# a\n# b\n\n# c\n# d\nx = 1\n", "py")
        assert block.lines == 4
        assert block.too_long

    def test_code_gap_does_not_merge(self) -> None:
        blocks = comment_blocks("# a\nx = 1\n# b\n", "py")
        assert [b.lines for b in blocks] == [1, 1]

    def test_block_doc_only_when_all_runs_doc(self) -> None:
        [block] = comment_blocks("/// doc\n\n// plain\npub fn f() {}\n", "rs")
        assert not block.doc


class TestTouchedCommentBlocks:
    def test_grown_block_reports_full_size(self) -> None:
        old = "# a\n# b\n# c\nx = 1\n"
        new = "# a\n# b\n# c\n# d\n# e\n# f\nx = 1\n"
        [block] = touched_comment_blocks(old, new, "py")
        assert block.lines == 6

    def test_untouched_block_absent(self) -> None:
        old = "# keep me here\n# and here too\nx = 1\n"
        new = "# keep me here\n# and here too\ny = 2\n"
        assert touched_comment_blocks(old, new, "py") == []

    def test_whitespace_reflow_absent(self) -> None:
        old = "# alpha\n# beta\n# gamma\nx = 1\n"
        new = "#  alpha\n#  beta\n#  gamma\nx = 1\n"
        assert touched_comment_blocks(old, new, "py") == []

    def test_duplicate_copy_is_touched(self) -> None:
        # One identical old block exempts one new copy; a second copy is touched.
        old = "# a\n# b\n# c\n# d\nx = 1\n"
        new = "# a\n# b\n# c\n# d\nx = 1\n# a\n# b\n# c\n# d\n"
        assert len(touched_comment_blocks(old, new, "py")) == 1

    def test_legacy_oversized_run_stays_exempt(self) -> None:
        old = "/*\n a\n b\n c\n d\n*/\nfn f() {}\n"
        new = "/*\n a\n b\n c\n d\n*/\nfn g() {}\n"
        assert touched_comment_blocks(old, new, "rs") == []

    def test_reflow_to_oversize_is_touched(self) -> None:
        # A short block comment reflowed over the line budget keeps its key but is no longer exempt.
        old = "fn f() {\n    /* one two three */\n}\n"
        new = "fn f() {\n    /*\n     one\n     two\n     three\n    */\n}\n"
        [block] = touched_comment_blocks(old, new, "rs")
        assert block.too_long


class TestLangForPath:
    def test_case_insensitive_suffix(self) -> None:
        assert lang_for_path(Path("module.PY")) == "py"
        assert lang_for_path(Path("main.GO")) == "go"
