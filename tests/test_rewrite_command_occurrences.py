from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.app import get_matching_hooks
from captain_hook.dispatch import execute_hook
from captain_hook.primitives.commands import Rewritten, WalkContext, rewrite_command_occurrences
from captain_hook.types import Action, Command
from tests.helpers import make_ctx, make_pre_tool_event


def fire(tmp_path: Path, command: str):
    ctx = make_ctx(tmp_path)
    evt = make_pre_tool_event("Bash", {"command": command}, ctx)
    return [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]


class TestRewriteCommandOccurrences:
    def test_single_occurrence_rewrite_preserves_siblings(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(
            to=lambda evt, occ: "ccx read foo.py --full" if occ.command.matches(r"^cat foo\.py$") else None,
        )
        result = next(r for r in fire(tmp_path, "cat foo.py; ls -la; cat bar.py") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "ccx read foo.py --full; ls -la; cat bar.py"}

    def test_two_occurrences_rewritten_with_one_merged_note(self, tmp_path: Path) -> None:
        def to(evt, occ):
            return f"ccx read {occ.command.args[0]} --full" if occ.command.matches(r"^cat ") else None

        rewrite_command_occurrences(to=to, note=lambda evt, pairs: f"Rewrote {len(pairs)} cat invocation(s)")
        result = next(r for r in fire(tmp_path, "cat foo.py; ls -la; cat bar.py") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "ccx read foo.py --full; ls -la; ccx read bar.py --full"}
        assert result.note == "Rewrote 2 cat invocation(s)"

    def test_block_if_overrides_rewritable_sibling(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(
            to=lambda evt, occ: "ccx read foo.py --full" if occ.command.matches(r"^cat foo\.py$") else None,
            block_if=lambda evt, occ: occ.command.matches(r"^git push"),
            block="Pushing is disabled",
        )
        result = next(r for r in fire(tmp_path, "cat foo.py; git push") if r)
        assert result.action == Action.block
        assert result.message == "Pushing is disabled"

    def test_zero_match_with_block_blocks(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(to=lambda evt, occ: None, block="Nothing matched")
        result = next(r for r in fire(tmp_path, "ls -la") if r)
        assert result.action == Action.block
        assert result.message == "Nothing matched"

    def test_zero_match_without_block_passes_through(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(to=lambda evt, occ: None)
        assert fire(tmp_path, "ls -la") == [None]

    def test_callable_note_receives_rewritten_pairs(self, tmp_path: Path) -> None:
        seen = []

        def note(evt, pairs):
            seen.append(pairs)
            return "noted"

        rewrite_command_occurrences(to=lambda evt, occ: "X" if occ.command.matches(r"^cat") else None, note=note)
        result = next(r for r in fire(tmp_path, "cat foo.py; ls") if r)
        assert result.note == "noted"
        assert len(seen) == 1
        [(occ, replacement)] = seen[0]
        assert replacement == "X"
        assert occ.command.raw == "cat foo.py"

    def test_idempotence_no_rewrite_when_splice_equals_raw(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(to=lambda evt, occ: occ.command.raw)
        assert fire(tmp_path, "cat foo.py; ls -la") == [None]

    def test_only_if_gates_registration(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(only_if=[Command(r"^cat\s")], to=lambda evt, occ: "X")
        assert not get_matching_hooks(make_pre_tool_event("Bash", {"command": "ls -la"}, make_ctx(tmp_path)))

    def test_span_none_occurrence_skipped_for_rewrite_but_visible_to_block_if(self, tmp_path: Path) -> None:
        to_calls: list[str] = []
        seen_spans: list[tuple[int, int] | None] = []

        def to(evt, occ):
            to_calls.append(occ.command.raw)
            return "ccx" if occ.command.matches(r"^git push$") else None

        def block_if(evt, occ):
            seen_spans.append(occ.command.span)
            return False

        rewrite_command_occurrences(to=to, block_if=block_if, block="unreachable")
        # "echo a >out b" absorbs the trailing word into the redirect, leaving it no
        # contiguous byte span — the span-less occurrence `to` must never see.
        result = next(r for r in fire(tmp_path, "echo a >out b; git push") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "echo a >out b; ccx"}
        assert to_calls == ["git push"]
        assert len(seen_spans) == 2
        assert None in seen_spans

    def test_registration_raises_without_block(self) -> None:
        with pytest.raises(TypeError, match="block="):
            rewrite_command_occurrences(to=lambda evt, occ: None, block_if=lambda evt, occ: True)

    def test_registration_raises_with_empty_block(self) -> None:
        with pytest.raises(TypeError, match="block="):
            rewrite_command_occurrences(to=lambda evt, occ: None, block_if=lambda evt, occ: True, block="")

    def test_callable_block_on_block_if_hit(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(
            to=lambda evt, occ: None,
            block_if=lambda evt, occ: occ.command.matches(r"^git push"),
            block=lambda evt, cl: f"Blocked line: {cl.raw}",
        )
        result = next(r for r in fire(tmp_path, "git push origin") if r)
        assert result.action == Action.block
        assert result.message == "Blocked line: git push origin"

    def test_callable_block_on_zero_rewrite_fallthrough(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(to=lambda evt, occ: None, block=lambda evt, cl: f"Nothing matched in: {cl.raw}")
        result = next(r for r in fire(tmp_path, "ls -la") if r)
        assert result.action == Action.block
        assert result.message == "Nothing matched in: ls -la"

    def test_callable_block_receives_evt_and_command_line(self, tmp_path: Path) -> None:
        seen = []

        def block(evt, cl):
            seen.append((evt, cl))
            return "blocked"

        rewrite_command_occurrences(to=lambda evt, occ: None, block=block)
        result = next(r for r in fire(tmp_path, "ls -la") if r)
        assert result.message == "blocked"
        assert len(seen) == 1
        [(evt, cl)] = seen
        assert cl.raw == "ls -la"
        assert evt.command == "ls -la"

    def test_str_block_unchanged(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(to=lambda evt, occ: None, block="static block")
        result = next(r for r in fire(tmp_path, "ls -la") if r)
        assert result.action == Action.block
        assert result.message == "static block"

    def test_callable_block_not_resolved_when_rewriting(self, tmp_path: Path) -> None:
        def block(evt, cl):
            raise AssertionError("block must not resolve on a successful rewrite")

        rewrite_command_occurrences(
            to=lambda evt, occ: "ccx read foo.py --full" if occ.command.matches(r"^cat foo\.py$") else None,
            block=block,
        )
        result = next(r for r in fire(tmp_path, "cat foo.py") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "ccx read foo.py --full"}


class TestVisitRewriteCommandOccurrences:
    def test_str_verdict_rewrites_occurrence(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(
            visit=lambda evt, occ, ctx: "ccx read foo.py --full" if occ.command.matches(r"^cat foo\.py$") else None,
        )
        result = next(r for r in fire(tmp_path, "cat foo.py; ls -la") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "ccx read foo.py --full; ls -la"}

    def test_rewritten_note_surfaces_in_rewrite(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(
            visit=lambda evt, occ, ctx: Rewritten("trash foo.txt", note="Rewrote rm to trash"),
        )
        result = next(r for r in fire(tmp_path, "rm foo.txt") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "trash foo.txt"}
        assert result.note == "Rewrote rm to trash"

    def test_duplicate_rewritten_notes_are_deduplicated(self, tmp_path: Path) -> None:
        rewrite_command_occurrences(
            visit=lambda evt, occ, ctx: Rewritten("trash file.txt", note="Rewrote rm to trash"),
        )
        result = next(r for r in fire(tmp_path, "rm a.txt; rm b.txt") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "trash file.txt; trash file.txt"}
        assert result.note == "Rewrote rm to trash"

    def test_hook_result_aborts_and_discards_accumulated_rewrite(self, tmp_path: Path) -> None:
        def visit(evt, occ, ctx):
            if occ.command.matches(r"^cat foo\.py$"):
                return "ccx read foo.py --full"
            return evt.block("Pushing is disabled") if occ.command.matches(r"^git push$") else None

        rewrite_command_occurrences(visit=visit)
        result = next(r for r in fire(tmp_path, "cat foo.py; git push") if r)
        assert result.action == Action.block
        assert result.message == "Pushing is disabled"
        assert result.updated_input is None

    def test_visit_sees_every_occurrence_in_order(self, tmp_path: Path) -> None:
        seen: list[tuple[str, WalkContext]] = []

        def visit(evt, occ, ctx):
            seen.append((occ.command.raw, ctx))
            return None

        rewrite_command_occurrences(visit=visit)
        assert fire(tmp_path, "echo a >out b; git push") == [None]
        assert [raw for raw, _ctx in seen] == ["echo a", "git push"]
        assert [ctx.spliceable for _raw, ctx in seen] == [False, True]

    def test_visit_threads_resolved_cd_cwd_after_visiting_cd(self, tmp_path: Path) -> None:
        seen: list[tuple[str, Path | None]] = []

        def visit(evt, occ, ctx):
            seen.append((occ.command.raw, ctx.cwd))
            return None

        rewrite_command_occurrences(visit=visit)
        evt = make_pre_tool_event("Bash", {"command": "cd /tmp && cmd"}, make_ctx(tmp_path))
        evt._raw["cwd"] = str(tmp_path)
        assert [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)] == [None]
        assert seen == [("cd /tmp", tmp_path), ("cmd", Path("/tmp").resolve())]

    def test_visit_does_not_thread_piped_cd_cwd(self, tmp_path: Path) -> None:
        seen: list[tuple[str, Path | None]] = []

        def visit(evt, occ, ctx):
            seen.append((occ.command.raw, ctx.cwd))
            return None

        rewrite_command_occurrences(visit=visit)
        evt = make_pre_tool_event("Bash", {"command": "cd /tmp | cmd"}, make_ctx(tmp_path))
        evt._raw["cwd"] = str(tmp_path)
        assert [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)] == [None]
        assert seen == [("cd /tmp", tmp_path), ("cmd", tmp_path)]

    def test_rewrite_for_backslash_continuation_raises(self, tmp_path: Path) -> None:
        spliceable: list[bool] = []

        def visit(evt, occ, ctx):
            spliceable.append(ctx.spliceable)
            return "printf a b"

        rewrite_command_occurrences(visit=visit)
        evt = make_pre_tool_event("Bash", {"command": "printf a \\\n b"}, make_ctx(tmp_path))
        [entry] = get_matching_hooks(evt)
        assert entry.handler is not None
        with pytest.raises(ValueError, match="rewrite for a non-spliceable occurrence"):
            entry.handler(evt)
        assert spliceable == [False]

    def test_registration_raises_with_visit_and_to(self) -> None:
        with pytest.raises(TypeError, match="takes either to= or visit=, not both or neither"):
            rewrite_command_occurrences(to=lambda evt, occ: None, visit=lambda evt, occ, ctx: None)

    def test_registration_raises_with_visit_and_block_if(self) -> None:
        with pytest.raises(TypeError, match="visit= form takes no block_if/block/note"):
            rewrite_command_occurrences(visit=lambda evt, occ, ctx: None, block_if=lambda evt, occ: True)

    def test_registration_raises_with_visit_and_note(self) -> None:
        with pytest.raises(TypeError, match="visit= form takes no block_if/block/note"):
            rewrite_command_occurrences(visit=lambda evt, occ, ctx: None, note="not allowed")

    def test_registration_raises_with_neither_form(self) -> None:
        with pytest.raises(TypeError, match="takes either to= or visit=, not both or neither"):
            rewrite_command_occurrences()
