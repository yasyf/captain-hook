from __future__ import annotations

import re
from collections.abc import Set
from dataclasses import dataclass
from typing import Any

import pytest

from captain_hook.ast_grep import COMMENT_TYPES
from captain_hook.contexts import (
    PIN_EXCERPT_CAP,
    WORKFLOW_SCRIPT_CAP,
    AfterEdit,
    BeforeEdit,
    Excerpts,
    Introduced,
    WorkflowScriptSource,
    apply_contexts,
    excerpt_around,
    with_defaults,
)
from captain_hook.packs.general.models import (
    ProseSpawn,
    ProseWorkflowScript,
    prose_deliverable_sentences,
)
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

    def test_write_at_post_tool_use_yields_none(self, tmp_path: Any) -> None:
        path = tmp_path / "landed.py"
        path.write_text("# note\nx = 1\n")
        evt = mock_event("PostToolUse", tool="Write", file=str(path), content="# note\nx = 1\n")
        assert Introduced(kind=COMMENT_TYPES).content(evt) is None

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

    def test_clipped_block_shows_elision_marker(self) -> None:
        evt = edit_event(old="y" * 500 + "\n", content="x = 2\n")  # 501-char pre-image
        prompt = apply_contexts(Prompt().system("s"), evt, [BeforeEdit()], max_len=100)
        assert prompt is not None
        assert "…(+401ch)" in str(prompt)


class TestWithDefaults:
    def test_defaults_appended_after_user_contexts(self) -> None:
        mine = Introduced(kind="comment")
        assert with_defaults([mine]) == (mine, BeforeEdit(), AfterEdit())

    def test_empty_yields_just_defaults(self) -> None:
        assert with_defaults(()) == (BeforeEdit(), AfterEdit())

    def test_user_instance_replaces_default_of_same_type(self) -> None:
        mine = BeforeEdit(required=True)
        assert with_defaults([mine]) == (mine, AfterEdit())


class TestProseDeliverableSentences:
    def test_writing_verb_governing_artifact_matches(self) -> None:
        assert prose_deliverable_sentences("Write the README quickstart for this repo")
        assert prose_deliverable_sentences("draft the release notes for v2")
        assert prose_deliverable_sentences("Update CHANGELOG.md with an entry for the retry fix")

    def test_negated_ask_is_screened_out(self) -> None:
        assert not prose_deliverable_sentences("Fix the test in cli.py. Do NOT edit the CHANGELOG — a sibling owns it")

    def test_mixed_prompt_keeps_the_positive_ask(self) -> None:
        got = prose_deliverable_sentences("Fix cli.py; do NOT edit CHANGELOG.md. Then draft the release notes.")
        assert got == ["Then draft the release notes."]

    def test_path_tokens_and_mere_mentions_do_not_match(self) -> None:
        assert not prose_deliverable_sentences("Update the retry backoff config per the spec in docs/plan.md")
        assert not prose_deliverable_sentences("Audit docs/architecture.md for stale claims")
        assert not prose_deliverable_sentences("all prose stays with the main agent on fable")
        assert not prose_deliverable_sentences("review the README for factual errors")


class TestProseSpawn:
    def test_prose_ask_renders_matched_sentences(self) -> None:
        evt = mock_tool_event(
            "Task", tool_input={"prompt": "Write the README quickstart for this repo", "model": "sonnet"}
        )
        content = ProseSpawn().content(evt)
        assert content is not None
        assert content.startswith("model: sonnet\n")
        assert "sentences the prose prefilter matched:\n  write the README quickstart" in content

    def test_non_prose_prompt_yields_none(self) -> None:
        evt = mock_tool_event("Task", tool_input={"prompt": "update the retry backoff config", "model": "opus"})
        assert ProseSpawn().content(evt) is None

    def test_negated_ask_yields_none(self) -> None:
        evt = mock_tool_event(
            "Task",
            tool_input={"prompt": "Fix cli.py. Do NOT edit the CHANGELOG — a sibling owns it", "model": "opus"},
        )
        assert ProseSpawn().content(evt) is None


class TestWorkflowScriptSource:
    def test_inline_script_quotes_pin_lines_and_prose(self) -> None:
        script = "agent('write the README intro', {label: 'docs', model: 'opus'})\nagent('fix the test')\n"
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = ProseWorkflowScript().content(evt)
        assert content is not None
        assert content.startswith("excerpts around every model pin in this script")
        assert "  agent('write the README intro', {label: 'docs', model: 'opus'})" in content
        assert "sentences the prose prefilter matched:" in content
        assert content.endswith(f"\n\n{script}")

    def test_long_line_pin_excerpt_shows_opts(self) -> None:
        script = (
            f"await agent('do a big thing with {'x' * 400}', "
            "{label: 'r4-find-codex', model: 'sonnet', effort: 'low'})"
        )
        assert len(script) > 200
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        header = content.split("\n\n", 1)[0]
        assert "label: 'r4-find-codex'" in header
        assert "model: 'sonnet'" in header
        assert "effort: 'low'" in header

    def test_minified_multi_pin_line_keeps_separate_windows(self) -> None:
        gap = "z" * 400
        script = (
            f"await agent('a {gap}', {{label: 'a', model: 'haiku'}}); "
            f"await agent('b {gap}', {{label: 'b', model: 'opus'}})"
        )
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        header = content.split("\n\n", 1)[0]
        assert "label: 'a'" in header and "model: 'haiku'" in header
        assert "label: 'b'" in header and "model: 'opus'" in header
        assert len([line for line in header.splitlines() if line.startswith("  ")]) == 2

    def test_pin_near_line_start_windows_forward_to_label(self) -> None:
        script = "agent('fix', {model: 'sonnet', label: 'lead'}) // " + "q" * 300
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        header = content.split("\n\n", 1)[0]
        excerpt = next(line for line in header.splitlines() if line.startswith("  agent("))
        assert excerpt.endswith("…")
        assert "label: 'lead'" in excerpt
        assert excerpt[2:-1] in script  # body is a verbatim substring; only the trailing … is inserted

    def test_indented_line_kept_verbatim(self) -> None:
        script = "  agent('write the README intro', {model: 'opus'})\n"
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        assert "    agent('write the README intro', {model: 'opus'})" in content  # 2-space prefix + source indent

    def test_indented_windowed_line_body_is_verbatim(self) -> None:
        line = "    await agent('do a thing with " + "x" * 400 + "', {label: 'deep', model: 'sonnet', effort: 'low'})"
        script = line + "\n"
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        header = content.split("\n\n", 1)[0]
        excerpt = next(hl for hl in header.splitlines() if "model: 'sonnet'" in hl)
        assert excerpt.startswith("  ")
        assert excerpt[2:].strip("…") in script  # body between prefix and elision markers is an exact substring
        assert "label: 'deep'" in excerpt

    def test_header_capped_with_marker_and_correct_count(self) -> None:
        pins = "".join(f"agent('s{i} {'q' * 400}', {{model: 'sonnet', label: 'p{i:02d}'}}); " for i in range(20))
        assert len(pins) < WORKFLOW_SCRIPT_CAP  # isolate the header cap from source truncation
        evt = mock_tool_event("Workflow", tool_input={"script": pins})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        header = content.split("\n\n", 1)[0]
        lead, pin_block = header.split("\n", 1)
        assert lead.startswith("excerpts around the first model pins in this script")
        assert "NOT necessarily unpinned" in lead
        block_lines = pin_block.splitlines()
        marker = block_lines[-1]
        excerpt_lines = block_lines[:-1]
        assert marker.startswith("  … [+") and marker.endswith(" more model pins not excerpted]")
        dropped = int(marker[len("  … [+") : marker.index(" more")])
        assert dropped == 20 - len(excerpt_lines)  # non-adjacent pins → one pin per window
        assert 0 < dropped < 20
        assert len("\n".join(excerpt_lines)) <= PIN_EXCERPT_CAP
        assert "label: 'p00'" in excerpt_lines[0]  # first windows intact

    def test_uncapped_header_is_byte_identical_no_marker(self) -> None:
        script = "agent('write the README intro', {label: 'docs', model: 'opus'})\n"
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        assert "more model pins not excerpted" not in content
        assert content.startswith(
            "excerpts around every model pin in this script (a stage not quoted here inherits the session model):"
        )

    def test_no_pins_says_none(self) -> None:
        evt = mock_tool_event("Workflow", tool_input={"script": "agent('write the README intro')"})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        assert "  (none)" in content

    def test_no_prose_ask_yields_none(self) -> None:
        evt = mock_tool_event("Workflow", tool_input={"script": "agent('fix the test', {model: 'opus'})"})
        assert ProseWorkflowScript().content(evt) is None
        base = WorkflowScriptSource().content(evt)
        assert base is not None
        assert "  agent('fix the test', {model: 'opus'})" in base

    def test_negated_prose_ask_yields_none(self) -> None:
        script = "agent('Fix the import. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'opus'})"
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        assert ProseWorkflowScript().content(evt) is None

    def test_script_path_reads_file(self, tmp_path: Any) -> None:
        p = tmp_path / "wf.js"
        p.write_text("agent('write the README intro', {model: 'sonnet'})")
        evt = mock_tool_event("Workflow", tool_input={"scriptPath": str(p)})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        assert "  agent('write the README intro', {model: 'sonnet'})" in content

    def test_truncation_keeps_marker_and_all_pin_lines(self) -> None:
        filler = "x = 1\n" * (WORKFLOW_SCRIPT_CAP // 6)
        script = (
            "agent('write the README intro', {model: 'opus'})\n"
            f"{filler}agent('mid', {{model: 'haiku'}})\nconst midMarker = 'ZZZMID'\n{filler}"
            "agent('end', {model: 'sonnet'})\n"
        )
        evt = mock_tool_event("Workflow", tool_input={"script": script})
        content = WorkflowScriptSource().content(evt)
        assert content is not None
        assert "script truncated" in content
        assert "agent('write the README intro'" in content
        assert "agent('end'" in content
        assert "  agent('mid', {model: 'haiku'})" in content
        assert "ZZZMID" not in content

    def test_missing_script_path_yields_none(self, tmp_path: Any) -> None:
        evt = mock_tool_event("Workflow", tool_input={"scriptPath": str(tmp_path / "missing.js")})
        assert WorkflowScriptSource().content(evt) is None


def model_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r"model: '\w+'", text)]


class TestExcerptAround:
    def test_short_line_quoted_whole(self) -> None:
        text = "agent('x', {model: 'opus'})"
        assert len(text) <= 200
        ex = excerpt_around(text, model_spans(text))
        assert ex.excerpts == (text,)
        assert ex.quoted == 1
        assert ex.dropped == 0
        assert not ex.capped

    def test_short_line_covers_all_its_spans_in_one_window(self) -> None:
        text = "alpha model: 'x' beta model: 'y' gamma"
        assert len(text) <= 200
        ex = excerpt_around(text, model_spans(text))
        assert ex.excerpts == (text,)  # one whole-line window covering both pins
        assert ex.quoted == 2

    def test_nearby_spans_on_long_line_merge(self) -> None:
        text = "x" * 250 + " model: 'a' model: 'b'"
        spans = model_spans(text)
        assert len(text) > 200 and len(spans) == 2
        ex = excerpt_around(text, spans)
        assert len(ex.excerpts) == 1
        assert ex.quoted == 2
        assert "model: 'a'" in ex.excerpts[0] and "model: 'b'" in ex.excerpts[0]
        assert ex.excerpts[0].startswith("…")  # text elided before the window

    def test_distant_spans_on_long_line_stay_separate(self) -> None:
        text = "model: 'a' " + "z" * 400 + " model: 'b'"
        spans = model_spans(text)
        assert len(text) > 200 and len(spans) == 2
        ex = excerpt_around(text, spans)
        assert len(ex.excerpts) == 2
        assert ex.quoted == 2
        assert "model: 'a'" in ex.excerpts[0]
        assert "model: 'b'" in ex.excerpts[1]
        assert ex.excerpts[0].endswith("…") and ex.excerpts[1].startswith("…")

    def test_windowed_excerpt_body_is_verbatim_substring(self) -> None:
        text = "model: 'lead' " + "q" * 300
        (ex,) = excerpt_around(text, model_spans(text)).excerpts
        assert not ex.startswith("…")  # window reaches the line start
        assert ex.endswith("…")
        assert ex.rstrip("…") in text

    def test_budget_caps_and_counts_dropped(self) -> None:
        text = "\n".join(f"model: 's{i}'" for i in range(10))
        spans = model_spans(text)
        assert len(spans) == 10
        ex = excerpt_around(text, spans, budget=30)
        assert ex.capped
        assert ex.quoted + ex.dropped == 10
        assert ex.quoted == 2 and ex.dropped == 8  # 11 + 1 + 11 = 23 fits; the third overflows 30
        block = ex.block("model pins")
        lines = block.splitlines()
        assert lines[0] == "  model: 's0'"
        assert lines[-1] == "  … [+8 more model pins not excerpted]"

    def test_first_excerpt_always_kept_even_over_budget(self) -> None:
        text = "\n".join(["model: 'first'", "model: 'second'"])
        ex = excerpt_around(text, model_spans(text), budget=1)
        assert ex.excerpts == ("model: 'first'",)
        assert ex.quoted == 1 and ex.dropped == 1

    def test_block_empty_renders_placeholder(self) -> None:
        ex = excerpt_around("no pins anywhere in this text", [])
        assert ex.excerpts == () and not ex.capped
        assert ex.block("model pins") == "  (none)"

    def test_block_custom_indent_and_empty(self) -> None:
        assert Excerpts((), 0, 0).block("things", indent="    ", empty="<empty>") == "    <empty>"

    def test_spans_from_re_finditer_group_by_line(self) -> None:
        text = "agent('a', {model: 'opus'})\nagent('b', {model: 'haiku'})"
        ex = excerpt_around(text, [m.span() for m in re.finditer(r"model: '\w+'", text)])
        assert ex.excerpts == ("agent('a', {model: 'opus'})", "agent('b', {model: 'haiku'})")
        assert ex.quoted == 2 and ex.dropped == 0
