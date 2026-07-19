from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import captain_hook
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack
from captain_hook.packs.general.deletions import in_vcs_repo, rm_targets, trash_binary
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.types import Event
from captain_hook.util.scratch import is_scratch_path
from captain_hook.util.shell import emit_token

PACKS_DIR = Path(captain_hook.__file__).parent / "packs"


@pytest.fixture
def no_scratch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
    monkeypatch.setattr("captain_hook.util.scratch.SCRATCH_DIR_NAMES", frozenset())


@pytest.fixture
def no_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name, **kw: None)


class TestBlockRiskyRm:
    def test_repo_boundaries_and_root(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")

        def decision(command: str, cwd: Path | None = None) -> dict[str, Any] | None:
            evt = input_to_event(
                Event.PreToolUse,
                Input(command=command, cwd=str(cwd) if cwd is not None else None),
            )
            return dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        git_dir = tmp_path / "git-dir"
        (git_dir / ".git").mkdir(parents=True)
        assert decision(f"rm {git_dir / 'file.txt'}", Path("/")) is None

        git_file = tmp_path / "git-file"
        git_file.mkdir()
        (git_file / ".git").write_text("gitdir: ../actual")
        assert decision(f"rm {git_file / 'file.txt'}", Path("/")) is None

        jj_dir = tmp_path / "jj-dir"
        (jj_dir / ".jj").mkdir(parents=True)
        assert decision(f"rm {jj_dir / 'file.txt'}", Path("/")) is None

        nested_root = tmp_path / "nested-root"
        (nested_root / ".git").mkdir(parents=True)
        assert decision(f"rm {nested_root / 'one' / 'two' / 'file.txt'}", Path("/")) is None

        outside = decision(f"rm {tmp_path / 'outside' / 'file.txt'}", Path("/"))
        assert outside is not None
        assert outside["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in outside["hookSpecificOutput"]["permissionDecisionReason"]

        sudo = decision(f"sudo rm {tmp_path / 'sudo-outside.txt'}", Path("/"))
        assert sudo is not None
        assert sudo["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in sudo["hookSpecificOutput"]["permissionDecisionReason"]

        repo_root = tmp_path / "whole-repo"
        (repo_root / ".git").mkdir(parents=True)
        root = decision(f"rm -rf {repo_root}", Path("/"))
        assert root is not None
        assert root["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "history" in root["hookSpecificOutput"]["permissionDecisionReason"]

    def test_normalizes_executable_and_path_token(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")

        def decision(command: str, cwd: Path) -> dict[str, Any] | None:
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(cwd)))
            return dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        uppercase = decision("RM /x", Path("/"))
        assert uppercase is not None
        assert uppercase["hookSpecificOutput"]["permissionDecision"] == "deny"

        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("")
        escaped = decision(r"rm ..\/outside/victim.txt", repo)
        assert escaped is not None
        assert escaped["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in escaped["hookSpecificOutput"]["permissionDecisionReason"]

        variable = decision("rm $FOO", outside)
        assert variable is not None
        assert variable["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in variable["hookSpecificOutput"]["permissionDecisionReason"]

    def test_leaf_symlink_semantics(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")

        def decision(command: str) -> dict[str, Any] | None:
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd="/"))
            return dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        target_repo = tmp_path / "target-repo"
        (target_repo / ".git").mkdir(parents=True)
        (target_repo / "real.txt").write_text("")
        outside_link = tmp_path / "outside-link"
        outside_link.symlink_to(target_repo / "real.txt")
        outside = decision(f"rm {outside_link}")
        assert outside is not None
        assert outside["hookSpecificOutput"]["permissionDecision"] == "deny"

        repo = tmp_path / "repo2"
        (repo / ".git").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "real.txt").write_text("")
        (repo / "link").symlink_to(elsewhere / "real.txt")
        assert decision(f"rm {repo / 'link'}") is None

    def test_glob_limit(self, isolate_modules: None, no_trash: None, tmp_path: Path) -> None:
        discover_pack("general", PACKS_DIR / "general")

        def decision(command: str, cwd: Path) -> dict[str, Any] | None:
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(cwd)))
            return dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        over = tmp_path / "over"
        over.mkdir()
        for index in range(11):
            (over / f"{index}.txt").write_text("")
        oversized = decision("rm *.txt", over)
        assert oversized is not None
        assert oversized["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "more than 10 files" in oversized["hookSpecificOutput"]["permissionDecisionReason"]

        boundary = tmp_path / "boundary"
        boundary.mkdir()
        for index in range(10):
            (boundary / f"{index}.txt").write_text("")
        assert decision("rm *.txt", boundary) is None

        recursive = tmp_path / "recursive"
        for index in range(11):
            path = recursive / str(index % 2) / f"{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        recursive_out = decision("rm **/*.txt", recursive)
        assert recursive_out is not None
        assert recursive_out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "more than 10 files" in recursive_out["hookSpecificOutput"]["permissionDecisionReason"]

        hidden = tmp_path / "hidden"
        hidden.mkdir()
        for index in range(11):
            (hidden / f".{index}").write_text("")
        for index in range(10):
            (hidden / str(index)).write_text("")
        assert decision("rm *", hidden) is None

        empty = tmp_path / "empty"
        empty.mkdir()
        assert decision("rm *.txt", empty) is None
        assert decision("rm $FOO", empty) is None

    def test_glob_preserves_trailing_slash(
        self,
        isolate_modules: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")

        def decision(command: str, cwd: Path) -> dict[str, Any] | None:
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(cwd)))
            return dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        entries = tmp_path / "entries"
        entries.mkdir()
        for index in range(3):
            (entries / f"dir-{index}").mkdir()
        for index in range(15):
            (entries / f"file-{index}.txt").write_text("")
        assert decision("rm */", entries) is None
        all_entries = decision("rm *", entries)
        assert all_entries is not None
        assert all_entries["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unknown_user_tilde(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        evt = input_to_event(
            Event.PreToolUse,
            Input(command="rm ~capt_hook_no_such_user_xyz/x", cwd="/"),
        )
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in result["hookSpecificOutput"]["permissionDecisionReason"]

    @pytest.mark.parametrize(
        ("walked", "expect_deny"),
        [
            pytest.param(20_000, True, id="at-budget"),
            pytest.param(19_999, False, id="under-budget"),
        ],
    )
    def test_recursive_glob_budget(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_trash: None,
        tmp_path: Path,
        walked: int,
        expect_deny: bool,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        anchors: list[Path] = []

        def fake_walked_paths(anchor: Path) -> Iterator[Path]:
            anchors.append(anchor)
            return (Path(f"/synthetic/{index}.txt") for index in range(walked))

        monkeypatch.setattr("captain_hook.util.globbing.walked_paths", fake_walked_paths)
        evt = input_to_event(Event.PreToolUse, Input(command="rm **/*.zzz", cwd=str(tmp_path)))
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert anchors == [tmp_path.resolve()]
        if expect_deny:
            assert result is not None
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert "too broad" in result["hookSpecificOutput"]["permissionDecisionReason"]
        else:
            assert result is None

    def test_recursive_glob_does_not_follow_symlinks(self, isolate_modules: None, tmp_path: Path) -> None:
        discover_pack("general", PACKS_DIR / "general")
        loop = tmp_path / "loop"
        nested = loop / "a"
        nested.mkdir(parents=True)
        (nested / "one.txt").write_text("")
        (nested / "again").symlink_to(nested, target_is_directory=True)
        evt = input_to_event(Event.PreToolUse, Input(command="rm **/*.txt", cwd=str(loop)))
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None

    def test_glob_validates_each_match(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        repo = tmp_path / "repo3"
        (repo / ".git").mkdir(parents=True)
        (repo / "child" / "match").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("")
        evt = input_to_event(
            Event.PreToolUse,
            Input(command="rm child/*/../../../outside/*", cwd=str(repo)),
        )
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_tracks_literal_cd(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        for command in ("cd / && rm somefile", "(cd / && rm somefile)"):
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(repo)))
            result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
            assert result is not None
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert "repository" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_tracks_temp_and_unknown_cd(self, isolate_modules: None, tmp_path: Path) -> None:
        discover_pack("general", PACKS_DIR / "general")
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        for command in ("cd /tmp && rm x", "cd $SOMEWHERE && rm x"):
            evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(repo)))
            assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None


class TestRmTrashRewrite:
    @staticmethod
    def load_with_trash(monkeypatch: pytest.MonkeyPatch, *, path: str = "/opt/fake/trash") -> None:
        discover_pack("general", PACKS_DIR / "general")
        monkeypatch.setattr(
            sys.modules["captain_hook._packs.general.deletions"],
            "trash_binary",
            lambda: path,
        )

    @staticmethod
    def decision(command: str, cwd: Path, session_dir: Path) -> dict[str, Any] | None:
        evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(cwd)))
        return dispatch(Event.PreToolUse, evt, session_dir=session_dir)

    @staticmethod
    def assert_rewrite(result: dict[str, Any] | None, command: str) -> dict[str, Any]:
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "allow"
        assert output["updatedInput"] == {"command": command}
        assert "Trash" in output["additionalContext"]
        return output

    def test_outside_vcs_rewrites(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.txt"
        victim.write_text("")
        output = self.assert_rewrite(
            self.decision(f"rm {victim}", Path("/"), tmp_path),
            f"/opt/fake/trash {victim}",
        )
        assert output["additionalContext"] == (
            f"Rewrote rm to trash: '{victim}' resolves outside any git/jj repository, so rm would be "
            "unrecoverable. The targets were moved to the macOS Trash instead — restorable via Finder (Put Back). "
            "If permanent deletion is truly intended, ask the user to run the rm themselves."
        )

    def test_flags_dropped(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        victim = tmp_path / "outside" / "victim.txt"
        victim.parent.mkdir()
        victim.write_text("")
        self.assert_rewrite(
            self.decision(f"rm -rf {victim}", Path("/"), tmp_path),
            f"/opt/fake/trash {victim}",
        )

    def test_no_trash_denies(
        self,
        isolate_modules: None,
        no_scratch: None,
        no_trash: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        victim = tmp_path / "outside" / "victim.txt"
        result = self.decision(f"rm {victim}", Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_repo_root_still_blocks(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        result = self.decision(f"rm -rf {repo}", Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "history" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_glob_limit_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for index in range(11):
            (outside / f"{index}.txt").write_text("")
        result = self.decision("rm *.txt", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert output["permissionDecisionReason"] == (
            "BLOCKED: the glob '*.txt' matches more than 10 files — an easy way to delete far more than intended. "
            "List the matches first (ls *.txt), narrow the pattern, or name a directory explicitly with rm -r <dir>."
        )
        assert "updatedInput" not in output

    def test_glob_within_limit_rewrites_raw_token(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for index in range(3):
            (outside / f"{index}.txt").write_text("")
        self.assert_rewrite(
            self.decision("rm *.txt", outside, tmp_path),
            "/opt/fake/trash *.txt",
        )

    def test_quoted_glob_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for index in range(3):
            (outside / f"{index}.txt").write_text("")
        result = self.decision("rm '*.txt'", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_glob_within_limit_containing_repo_root_blocks(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        repo = outside / "repo"
        (repo / ".git").mkdir(parents=True)
        for index in range(3):
            (outside / f"entry-{index}").write_text("")
        result = self.decision("rm -rf *", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "history" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_glob_over_limit_blocks_before_uncollected_repo_root(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        for index in range(12):
            (outside / f"entry-{index}").write_text("")
        repo = outside / "repo"
        (repo / ".git").mkdir(parents=True)
        deletions = sys.modules["captain_hook._packs.general.deletions"]

        def ordered_iglob(
            pattern: str,
            *,
            root_dir: str,
            recursive: bool,
            include_hidden: bool,
        ) -> Iterator[str]:
            assert (pattern, root_dir, recursive, include_hidden) == ("*", str(outside), False, False)
            return iter([*(f"entry-{index}" for index in range(12)), repo.name])

        monkeypatch.setattr(deletions.glob, "iglob", ordered_iglob)
        expansion = deletions.glob_matches("*", outside, limit=10)
        assert len(expansion.matches) == 11
        assert repo.name not in expansion.matches

        result = self.decision("rm -rf *", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert output["permissionDecisionReason"] == (
            "BLOCKED: the glob '*' matches more than 10 files — an easy way to delete far more than intended. "
            "List the matches first (ls *), narrow the pattern, or name a directory explicitly with rm -r <dir>."
        )
        assert "updatedInput" not in output

    def test_too_broad_glob_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)

        def fake_walked_paths(anchor: Path) -> Iterator[Path]:
            return (Path(f"/synthetic/{index}.txt") for index in range(20_000))

        monkeypatch.setattr("captain_hook.util.globbing.walked_paths", fake_walked_paths)
        result = self.decision("rm **/*.zzz", tmp_path, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert output["permissionDecisionReason"] == (
            "BLOCKED: the glob '**/*.zzz' is too broad to verify safely (the scan budget was exhausted before it "
            "completed). Narrow the pattern or delete a specific directory with rm -r <dir>."
        )
        assert "updatedInput" not in output

    def test_cd_prefix_preserved(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("")
        self.assert_rewrite(
            self.decision(f"cd {outside} && rm victim.txt", tmp_path, tmp_path),
            f"cd {outside} && /opt/fake/trash victim.txt",
        )

    def test_existing_cd_changes_rm_cwd(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        self.assert_rewrite(
            self.decision(f"cd {outside} && rm -rf .", tmp_path, tmp_path),
            f"cd {outside} && /opt/fake/trash .",
        )

    def test_failed_cd_keeps_prior_cwd(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        missing = tmp_path / "nonexistent-xyz"
        result = self.decision(f"cd {missing} && rm -rf .", repo, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository root" in output["permissionDecisionReason"]

    def test_piped_cd_does_not_change_rm_cwd(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        command = f"cd {repo} | rm -rf notes"
        self.assert_rewrite(
            self.decision(command, outside, tmp_path),
            f"cd {repo} | /opt/fake/trash notes",
        )

    def test_multi_occurrence_splice(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        first = tmp_path / "outside-one" / "x"
        second = tmp_path / "outside-two" / "y"
        first.parent.mkdir()
        second.parent.mkdir()
        command = f"rm {first} && echo hi && rm {second}"
        self.assert_rewrite(
            self.decision(command, Path("/"), tmp_path),
            f"/opt/fake/trash {first} && echo hi && /opt/fake/trash {second}",
        )

    def test_block_wins_over_rewrite(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside" / "x"
        repo = tmp_path / "repo"
        outside.parent.mkdir()
        (repo / ".git").mkdir(parents=True)
        result = self.decision(f"rm {outside} && rm -rf {repo}", Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "history" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_dash_target_prefixed(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "-victim.txt").write_text("")
        self.assert_rewrite(
            self.decision("rm -- -victim.txt", outside, tmp_path),
            "/opt/fake/trash ./-victim.txt",
        )

    def test_dequoted_space_target_requoted(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "my victim.txt").write_text("")
        self.assert_rewrite(
            self.decision("rm 'my victim.txt'", outside, tmp_path),
            "/opt/fake/trash 'my victim.txt'",
        )

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("rm '~/victim'", id="quoted-tilde"),
            pytest.param(r"rm 'foo\ bar'", id="quoted-backslash"),
            pytest.param("rm /outside/{a,b}", id="braces"),
            pytest.param("rm foo\\\nbar", id="backslash-newline"),
        ],
    )
    def test_unsafe_emission_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
        command: str,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        result = self.decision(command, outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_trash_binary_path_is_quoted(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch, path="/opt/fake dir/trash")
        victim = tmp_path / "outside" / "victim.txt"
        victim.parent.mkdir()
        output = self.assert_rewrite(
            self.decision(f"rm {victim}", Path("/"), tmp_path),
            f"'/opt/fake dir/trash' {victim}",
        )
        assert output["updatedInput"]["command"].startswith("'/opt/fake dir/trash' ")

    def test_variable_target_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        result = self.decision("rm $FOO", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_allowed_rm_untouched(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        self.load_with_trash(monkeypatch)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        victim = repo / "file.txt"
        victim.write_text("")
        assert self.decision(f"rm {victim}", Path("/"), tmp_path) is None


class TestBlastRadius:
    @pytest.mark.parametrize("command", ["rm -rf --no-preserve-root /", "rm -rf /", "rm -rf /.."])
    def test_filesystem_root(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
        command: str,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        result = TestRmTrashRewrite.decision(command, Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "filesystem root" in output["permissionDecisionReason"]

    def test_home_directories(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        nested = fake_home / "sub"
        nested.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        for home in (fake_home, nested / "..", Path("/Users"), Path("/Users/somebody")):
            result = TestRmTrashRewrite.decision(f"rm -rf {home}", Path("/"), tmp_path)
            assert result is not None
            output = result["hookSpecificOutput"]
            assert output["permissionDecision"] == "deny"
            assert "home directory" in output["permissionDecisionReason"]

    @pytest.mark.parametrize("marker_type", ["directory", "file"])
    def test_repo_containing_directory(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
        marker_type: str,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        target = tmp_path / f"contains-repo-{marker_type}"
        marker = target / "proj" / ".git"
        match marker_type:
            case "directory":
                marker.mkdir(parents=True)
            case "file":
                marker.parent.mkdir(parents=True)
                marker.write_text("gitdir: ../actual")
        result = TestRmTrashRewrite.decision(f"rm -rf {target}", Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "contains git/jj repositories" in output["permissionDecisionReason"]

    def test_clean_directory_rewrites(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        target = tmp_path / "clean"
        target.mkdir()
        TestRmTrashRewrite.assert_rewrite(
            TestRmTrashRewrite.decision(f"rm -rf {target}", Path("/"), tmp_path),
            f"/opt/fake/trash {target}",
        )

    def test_symlink_leaf_rewrites_without_scan(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        target = tmp_path / "target"
        (target / "proj" / ".git").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(
            "captain_hook.util.vcs.scanned_names",
            lambda _anchor: pytest.fail("scanned a symlink leaf"),
        )
        TestRmTrashRewrite.assert_rewrite(
            TestRmTrashRewrite.decision(f"rm -rf {link}", Path("/"), tmp_path),
            f"/opt/fake/trash {link}",
        )

    def test_glob_match_containing_repo_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        (outside / "container" / "proj" / ".git").mkdir(parents=True)
        result = TestRmTrashRewrite.decision("rm -rf *", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "contains git/jj repositories" in output["permissionDecisionReason"]

    def test_non_rewritable_does_not_scan(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        deletions = sys.modules["captain_hook._packs.general.deletions"]
        monkeypatch.setattr(deletions, "trash_binary", lambda: None)
        monkeypatch.setattr(deletions, "contains_repo", lambda _path: pytest.fail("scanned without trash"))
        target = tmp_path / "contains-repo"
        (target / "proj" / ".git").mkdir(parents=True)
        result = TestRmTrashRewrite.decision(f"rm -rf {target}", Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]

    def test_file_rewrites_without_scan(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        deletions = sys.modules["captain_hook._packs.general.deletions"]
        monkeypatch.setattr(deletions, "contains_repo", lambda _path: pytest.fail("scanned a plain file"))
        target = tmp_path / "plain.txt"
        target.write_text("")
        TestRmTrashRewrite.assert_rewrite(
            TestRmTrashRewrite.decision(f"rm {target}", Path("/"), tmp_path),
            f"/opt/fake/trash {target}",
        )


class TestNestedDescent:
    @pytest.mark.parametrize(
        "command",
        [
            "bash -c 'rm -rf /'",
            "bash -lc 'rm -rf /'",
            "sh -c 'rm {target}'",
            "eval rm {target}",
        ],
    )
    def test_nested_rm_denies(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
        command: str,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        target = tmp_path / "outside.txt"
        target.write_text("")
        result = TestRmTrashRewrite.decision(command.format(target=target), Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]
        assert "updatedInput" not in output

    def test_nested_scratch_rm_allows(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        assert TestRmTrashRewrite.decision("bash -c 'rm /tmp/x'", Path("/"), tmp_path) is None

    def test_nested_cd_tracks_cwd(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "x").write_text("")
        result = TestRmTrashRewrite.decision(f'bash -c "cd {outside} && rm x"', Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]

    def test_nested_piped_cd_does_not_change_rm_cwd(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        payload = shlex.quote(f"cd {repo} | rm -rf notes")
        result = TestRmTrashRewrite.decision(f"bash -c {payload}", outside, tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]

    def test_depth_cap_fails_open(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        target = tmp_path / "outside.txt"
        target.write_text("")
        command = f"rm {target}"
        # NESTED_COMMAND_DEPTH=3 descends four bash -c layers (14.6's structured -c payload parts
        # reach one deeper than pre-14.6); the fifth wrapper is past the cap and fails open.
        for _ in range(5):
            command = f"bash -c {shlex.quote(command)}"
        assert TestRmTrashRewrite.decision(command, Path("/"), tmp_path) is None

    def test_nested_root_denies_without_trash(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        monkeypatch.setattr(sys.modules["captain_hook._packs.general.deletions"], "trash_binary", lambda: None)
        result = TestRmTrashRewrite.decision("bash -c 'rm -rf /'", Path("/"), tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "repository" in output["permissionDecisionReason"]

    def test_top_level_rewrite_and_cd_unchanged(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        no_scratch: None,
        tmp_path: Path,
    ) -> None:
        TestRmTrashRewrite.load_with_trash(monkeypatch)
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "x"
        target.write_text("")
        TestRmTrashRewrite.assert_rewrite(
            TestRmTrashRewrite.decision(f"rm {target}", Path("/"), tmp_path),
            f"/opt/fake/trash {target}",
        )
        TestRmTrashRewrite.assert_rewrite(
            TestRmTrashRewrite.decision(f"cd {outside} && rm x", tmp_path, tmp_path),
            f"cd {outside} && /opt/fake/trash x",
        )


class TestEmitTarget:
    @pytest.mark.parametrize(
        ("raw", "plain_words", "expected"),
        [
            pytest.param("file.txt", True, "file.txt", id="safe-word"),
            pytest.param("-victim.txt", True, "./-victim.txt", id="dash-leading"),
            pytest.param("*.txt", True, "*.txt", id="plain-glob"),
            pytest.param("*.txt", False, None, id="quoted-glob"),
            pytest.param("~/victim", True, "~/victim", id="plain-tilde"),
            pytest.param("~/victim", False, None, id="quoted-tilde"),
            pytest.param("my victim.txt", False, "'my victim.txt'", id="dequoted-space"),
            pytest.param(r"foo\ bar", True, "'foo bar'", id="plain-backslash"),
            pytest.param(r"foo\ bar", False, None, id="quoted-backslash"),
            pytest.param(r"\*.txt", True, None, id="escaped-glob"),
            pytest.param("victim{1,2}", True, None, id="braces"),
            pytest.param("$FOO", True, None, id="variable"),
            pytest.param("`find victim`", True, None, id="backticks"),
            pytest.param('foo"$BAR"', False, None, id="concatenation"),
        ],
    )
    def test_emission(self, raw: str, plain_words: bool, expected: str | None) -> None:
        assert emit_token(raw, plain_words=plain_words) == expected


@pytest.mark.parametrize(
    ("platform", "resolved", "expected"),
    [
        pytest.param("darwin", "/opt/fake/trash", "/opt/fake/trash", id="darwin-found"),
        pytest.param("darwin", None, None, id="darwin-missing"),
        pytest.param("linux", "/opt/fake/trash", None, id="linux-found"),
        pytest.param("linux", None, None, id="linux-missing"),
    ],
)
def test_trash_binary(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    resolved: str | None,
    expected: str | None,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: resolved)
    assert trash_binary() == expected


class TestIsScratchPath:
    @pytest.mark.parametrize(
        ("resolved", "expected"),
        [
            pytest.param(Path("/tmp/sweep_arc.py").resolve(), True, id="temp-root"),
            pytest.param(Path("/private/tmp/conf.py").resolve(), True, id="private-temp-root"),
            pytest.param(Path("/var/folders/ab/cd/T/scratch.json").resolve(), True, id="var-folders"),
            pytest.param(
                (Path("/a/b/c/d") / "../../../../tmp/sweep_arc.py").resolve(),
                True,
                id="relative-with-cwd",
            ),
            pytest.param(Path("/Users/u/proj/scratchpads/notes.md").resolve(), True, id="scratch-ancestor"),
            pytest.param(Path("/Users/u/proj/src/main.py").resolve(), False, id="non-scratch"),
            pytest.param(Path("/Users/u/proj/tmp").resolve(), False, id="file-named-tmp"),
            pytest.param(Path(f"/run/user/{os.getuid()}/app.tmp"), True, id="user-runtime-root"),
            pytest.param(Path(f"/run/user/{os.getuid() + 1}/x"), False, id="other-user-runtime-root"),
        ],
    )
    def test_paths(self, resolved: Path, expected: bool) -> None:
        assert is_scratch_path(resolved) is expected


class TestInVcsRepo:
    def test_markers(self, tmp_path: Path) -> None:
        git_dir = tmp_path / "git-dir"
        (git_dir / ".git").mkdir(parents=True)
        assert in_vcs_repo(git_dir) is True
        assert in_vcs_repo(git_dir / "one" / "two") is True

        git_file = tmp_path / "git-file"
        git_file.mkdir()
        (git_file / ".git").write_text("gitdir: ../actual")
        assert in_vcs_repo(git_file) is True

        jj_dir = tmp_path / "jj-dir"
        (jj_dir / ".jj").mkdir(parents=True)
        assert in_vcs_repo(jj_dir) is True
        assert in_vcs_repo(tmp_path / "absent") is False


class TestRmTargets:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            pytest.param(("-rf", "a", "b"), ["a", "b"], id="flags"),
            pytest.param(("--", "-weird", "b"), ["-weird", "b"], id="separator"),
            pytest.param(("/victim", "--"), ["/victim"], id="trailing-separator"),
            pytest.param(("-rf", "a", "--", "-b"), ["a", "-b"], id="operands-around-separator"),
            pytest.param(("-",), ["-"], id="bare-dash"),
            pytest.param((), [], id="empty"),
        ],
    )
    def test_targets(self, args: tuple[str, ...], expected: list[str]) -> None:
        assert rm_targets(args) == expected
