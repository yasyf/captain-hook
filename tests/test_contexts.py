from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from typing import Any

import pytest

from captain_hook.ast_grep import COMMENT_TYPES
from captain_hook.contexts import AfterEdit, BeforeEdit, Introduced, apply_contexts, with_defaults
from captain_hook.prompt import Prompt
from captain_hook.testing import FileFixture, Input
from captain_hook.testing.helpers import input_to_event, mock_event, mock_tool_event
from captain_hook.types import Event


@dataclass(frozen=True, slots=True)
class TombstoneNotes(Introduced):
    kind: str | Set[str] | None = COMMENT_TYPES

    def keep(self, text: str) -> bool:
        return "gone" in text


def edit_event(old: str = "x = 1\n", content: str = "x = 2\n", file: str = "a.py") -> Any:
    return mock_event("PreToolUse", tool="Edit", file=file, old=old, content=content)


class TestBeforeEdit:
    def test_defaults(self) -> None:
        assert BeforeEdit().tag == "before_edit"
        assert BeforeEdit().required is False

    def test_edit_pre_image(self) -> None:
        assert BeforeEdit().content(edit_event(old="x = 1\n")) == "x = 1\n"

    def test_multi_edit_joins_olds(self) -> None:
        evt = mock_tool_event(
            "MultiEdit",
            tool_input={
                "file_path": "a.py",
                "edits": [
                    {"old_string": "one", "new_string": "1"},
                    {"old_string": "two", "new_string": "2"},
                ],
            },
        )
        assert BeforeEdit().content(evt) == "one\ntwo"

    def test_write_reads_disk(self) -> None:
        evt = input_to_event(
            Event.PreToolUse,
            Input(tool="Write", file=FileFixture(name="ctx_before.py", content="on disk\n"), content="new\n"),
        )
        assert BeforeEdit().content(evt) == "on disk\n"

    def test_write_new_file_is_empty(self, tmp_path: Any) -> None:
        evt = mock_event("PreToolUse", tool="Write", file=str(tmp_path / "missing.py"), content="new\n")
        assert BeforeEdit().content(evt) == ""

    def test_non_tool_event_yields_none(self) -> None:
        assert BeforeEdit().content(mock_event("Stop")) is None


class TestAfterEdit:
    def test_defaults(self) -> None:
        assert AfterEdit().tag == "after_edit"
        assert AfterEdit().required is False

    def test_edit_new_text(self) -> None:
        assert AfterEdit().content(edit_event(content="x = 2\n")) == "x = 2\n"

    def test_write_new_text(self, tmp_path: Any) -> None:
        evt = mock_event("PreToolUse", tool="Write", file=str(tmp_path / "b.py"), content="written\n")
        assert AfterEdit().content(evt) == "written\n"

    def test_bash_yields_none(self) -> None:
        assert AfterEdit().content(mock_event("PreToolUse", tool="Bash", command="ls")) is None

    def test_non_tool_event_yields_none(self) -> None:
        assert AfterEdit().content(mock_event("Stop")) is None


class TestIntroduced:
    @pytest.mark.parametrize(
        "kwargs",
        [pytest.param({}, id="neither"), pytest.param({"kind": "comment", "pattern": "print($$$)"}, id="both")],
    )
    def test_exactly_one_of_kind_or_pattern(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Introduced(**kwargs)

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            pytest.param("comment", frozenset({"comment"}), id="bare_str"),
            pytest.param({"comment", "line_comment"}, frozenset({"comment", "line_comment"}), id="plain_set"),
            pytest.param(COMMENT_TYPES, COMMENT_TYPES, id="frozenset_passthrough"),
        ],
    )
    def test_kind_normalizes_to_frozenset(self, kind: Any, expected: frozenset[str]) -> None:
        assert Introduced(kind=kind).kind == expected
        assert isinstance(Introduced(kind=kind).kind, frozenset)

    def test_required_defaults_true(self) -> None:
        assert Introduced(kind="comment").required is True

    def test_tag_auto_derives_snake_case(self) -> None:
        assert Introduced(kind="comment").tag == "introduced"
        assert TombstoneNotes().tag == "tombstone_notes"

    def test_explicit_tag_wins(self) -> None:
        assert Introduced(kind="comment", tag="suspects").tag == "suspects"

    def test_kind_extracts_new_comments(self) -> None:
        evt = edit_event(old="x = 1\n", content="# fresh\nx = 2\n")
        assert Introduced(kind=COMMENT_TYPES).content(evt) == "# fresh"

    def test_pattern_extracts_new_matches(self) -> None:
        evt = edit_event(old="x = 1\n", content="print('hi')\nx = 1\n")
        assert Introduced(pattern="print($$$)").content(evt) == "print('hi')"

    def test_preexisting_construct_not_introduced(self) -> None:
        evt = edit_event(old="# fresh\nx = 1\n", content="# fresh\nx = 2\n")
        assert Introduced(kind=COMMENT_TYPES).content(evt) == ""

    def test_write_diffs_against_disk(self) -> None:
        evt = input_to_event(
            Event.PreToolUse,
            Input(
                tool="Write",
                file=FileFixture(name="ctx_intro.py", content="# already here\nx = 1\n"),
                content="# already here\n# brand new\nx = 2\n",
            ),
        )
        assert Introduced(kind=COMMENT_TYPES).content(evt) == "# brand new"

    def test_write_new_file_counts_everything(self, tmp_path: Any) -> None:
        evt = mock_event("PreToolUse", tool="Write", file=str(tmp_path / "new.py"), content="# note\nx = 1\n")
        assert Introduced(kind=COMMENT_TYPES).content(evt) == "# note"

    def test_multi_edit_diffs_joined_spans(self) -> None:
        evt = mock_tool_event(
            "MultiEdit",
            tool_input={
                "file_path": "a.py",
                "edits": [{"old_string": "x = 1", "new_string": "# why\nx = 2"}],
            },
        )
        assert Introduced(kind=COMMENT_TYPES).content(evt) == "# why"

    def test_unsupported_suffix_yields_none(self) -> None:
        evt = edit_event(old="x\n", content="# note\ny\n", file="notes.md")
        assert Introduced(kind=COMMENT_TYPES).content(evt) is None

    def test_bash_yields_none(self) -> None:
        assert Introduced(kind="comment").content(mock_event("PreToolUse", tool="Bash", command="ls")) is None

    def test_non_tool_event_yields_none(self) -> None:
        assert Introduced(kind="comment").content(mock_event("Stop")) is None

    def test_keep_filters_matches(self) -> None:
        evt = edit_event(old="x = 1\n", content="# gone now\n# still relevant\nx = 2\n")
        assert TombstoneNotes().content(evt) == "# gone now"


class TestApplyContexts:
    def test_required_empty_returns_none(self) -> None:
        assert apply_contexts(Prompt().system("s"), edit_event(), [Introduced(kind=COMMENT_TYPES)]) is None

    def test_optional_empty_block_omitted(self) -> None:
        prompt = apply_contexts(Prompt().system("s"), mock_event("Stop"), [BeforeEdit(), AfterEdit()])
        assert prompt is not None
        assert str(prompt) == "s"

    def test_blocks_append_in_array_order(self) -> None:
        evt = edit_event(old="x = 1\n", content="# note\nprint('hi')\nx = 2\n")
        prompt = apply_contexts(
            Prompt().system("s"),
            evt,
            [Introduced(pattern="print($$$)", tag="prints"), Introduced(kind=COMMENT_TYPES)],
        )
        assert prompt is not None
        rendered = str(prompt)
        assert rendered.index("<prints>") < rendered.index("<introduced>")

    def test_blocks_capped_at_max_len(self) -> None:
        evt = edit_event(old="y" * 500 + "\n", content="x = 2\n")
        prompt = apply_contexts(Prompt().system("s"), evt, [BeforeEdit()], max_len=100)
        assert prompt is not None
        assert "y" * 100 in str(prompt)
        assert "y" * 101 not in str(prompt)


class TestWithDefaults:
    def test_defaults_appended_after_user_contexts(self) -> None:
        mine = Introduced(kind="comment")
        assert with_defaults([mine]) == (mine, BeforeEdit(), AfterEdit())

    def test_empty_yields_just_defaults(self) -> None:
        assert with_defaults(()) == (BeforeEdit(), AfterEdit())

    def test_user_instance_replaces_default_of_same_type(self) -> None:
        mine = BeforeEdit(required=True)
        assert with_defaults([mine]) == (mine, AfterEdit())
