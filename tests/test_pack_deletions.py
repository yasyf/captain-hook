from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

import captain_hook
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack
from captain_hook.packs.general.deletions import in_vcs_repo, rm_targets
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.types import Event
from captain_hook.util.scratch import is_scratch_path

PACKS_DIR = Path(captain_hook.__file__).parent / "packs"


class TestBlockRiskyRm:
    def test_repo_boundaries_and_root(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
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
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
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
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
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

    def test_glob_limit(self, isolate_modules: None, tmp_path: Path) -> None:
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

    def test_glob_preserves_trailing_slash(self, isolate_modules: None, tmp_path: Path) -> None:
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
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
        discover_pack("general", PACKS_DIR / "general")
        evt = input_to_event(
            Event.PreToolUse,
            Input(command="rm ~capt_hook_no_such_user_xyz/x", cwd="/"),
        )
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "repository" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_recursive_glob_budget(
        self,
        isolate_modules: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        discover_pack("general", PACKS_DIR / "general")
        monkeypatch.setattr(sys.modules["captain_hook._packs.general.deletions"], "WALK_BUDGET", 50)
        broad = tmp_path / "broad"
        broad.mkdir()
        for index in range(100):
            directory = broad / str(index)
            directory.mkdir()
            (directory / "file.txt").write_text("")
        evt = input_to_event(Event.PreToolUse, Input(command="rm **/*.zzz", cwd=str(broad)))
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "too broad" in result["hookSpecificOutput"]["permissionDecisionReason"]

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
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
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
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", ())
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
