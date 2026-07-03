from __future__ import annotations

import pytest

from captain_hook.ast_grep import (
    COMMENT_TYPES,
    Match,
    SyntaxNode,
    comments,
    find_introduced,
    find_kinds,
    introduced_comments,
    parse,
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
