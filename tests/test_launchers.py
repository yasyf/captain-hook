"""Launcher tamper repair against a real local git origin standing in for GitHub.

The origin ships ``captain_hook/bin/hook`` as a symlink at ``v1.0.0`` and as an executable file at a
later commit, so a restore has to reproduce both shapes. Only the remote url, the Claude config dir,
and the state dir are redirected; git, the filesystem, and :mod:`captain_hook.update.launchers` are real.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from captain_hook import faults
from captain_hook.update import launchers, updater
from captain_hook.util.paths import resolve_claude_config_dir

STUB = b'#!/bin/bash\ncase "${1-}" in run) exit 0 ;; esac\nexec install-binary.sh "$@"\n'
SHIPPED_FILE = b'#!/bin/bash\n. "${0%/*}/../scripts/install-binary.sh"\n'
SYMLINK_TARGET = "../scripts/install-binary.sh"


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    repo = tmp_path / "origin"
    (bindir := repo / "captain_hook" / "bin").mkdir(parents=True)
    run_git(repo, "init", "-q")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("uploadpack.allowFilter", "true"),
        ("uploadpack.allowAnySHA1InWant", "true"),
    ):
        run_git(repo, "config", key, value)
    (bindir / "hook").symlink_to(SYMLINK_TARGET)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "symlink launcher")
    run_git(repo, "tag", "v1.0.0")
    (bindir / "hook").unlink()
    (bindir / "hook").write_bytes(SHIPPED_FILE)
    (bindir / "hook").chmod(0o755)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "file launcher")
    monkeypatch.setattr(launchers, "REMOTE", f"file://{repo}")
    return {"file": run_git(repo, "rev-parse", "HEAD")}


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def version_dir(version: str, launcher: bytes | None, *, pids: tuple[int, ...] = ()) -> Path:
    (bindir := launchers.cache_root() / version / "bin").mkdir(parents=True)
    if launcher is not None:
        (bindir / "hook").write_bytes(launcher)
        (bindir / "hook").chmod(0o755)
    for pid in pids:
        (marks := bindir.parent / ".in_use").mkdir(exist_ok=True)
        (marks / str(pid)).write_text(json.dumps({"pid": pid}))
    return bindir.parent


def install(*entries: tuple[Path, str | None]) -> None:
    roster = {
        "plugins": {
            launchers.ROSTER_ID: [
                {"installPath": str(p)} | ({"gitCommitSha": sha} if sha else {}) for p, sha in entries
            ]
        }
    }
    (resolve_claude_config_dir() / "plugins" / "installed_plugins.json").write_text(json.dumps(roster))


def test_check_restores_every_tampered_launcher_a_session_can_reach(origin: dict[str, str]) -> None:
    live = version_dir("1.0.0", b"#!/bin/sh\nexit 0\n", pids=(os.getpid(),))
    installed = version_dir("2.0.0", STUB)
    intact = version_dir("3.0.0", SHIPPED_FILE)
    abandoned = version_dir("0.9.0", b"#!/bin/sh\nexit 0\n", pids=(dead_pid(),))
    install((installed, origin["file"]), (intact, origin["file"]))
    intact_inode = (intact / "bin" / "hook").stat().st_ino

    assert launchers.check_launchers() == ["1.0.0", "2.0.0"]

    assert os.readlink(live / "bin" / "hook") == SYMLINK_TARGET
    assert (installed / "bin" / "hook").read_bytes() == SHIPPED_FILE
    assert stat.S_IMODE((installed / "bin" / "hook").stat().st_mode) == 0o755
    assert (intact / "bin" / "hook").stat().st_ino == intact_inode
    assert (abandoned / "bin" / "hook").read_bytes() == b"#!/bin/sh\nexit 0\n"
    (announcement,) = faults.drain("/any/project")
    assert "restored the shipped bin/hook of captain-hook 1.0.0, 2.0.0" in announcement
    assert "`capt-hook pause --for <duration>`" in announcement
    log = updater.update_log_path().read_text()
    assert "launcher restored: 1.0.0 bin/hook was file" in log
    assert "shipped symlink" in log


def test_a_clean_recheck_runs_no_git(origin: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    install((version_dir("2.0.0", STUB), origin["file"]))
    assert launchers.check_launchers() == ["2.0.0"]

    def no_git(*args: str, **kwargs: object) -> None:
        raise AssertionError(f"git ran: {args}")

    monkeypatch.setattr(launchers, "git", no_git)

    assert launchers.check_launchers() == []
    assert faults.drain() != []
    assert faults.drain() == []


def test_a_ref_git_cannot_resolve_is_skipped_without_blocking_the_rest(origin: dict[str, str]) -> None:
    unknown = version_dir("4.0.0", STUB)
    install((unknown, "0" * 40), (version_dir("2.0.0", STUB), origin["file"]))

    assert launchers.check_launchers() == ["2.0.0"]

    assert (unknown / "bin" / "hook").read_bytes() == STUB
    assert f"cannot resolve what {'0' * 40} shipped" in updater.update_log_path().read_text()


def test_an_install_without_a_commit_falls_back_to_its_release_tag(origin: dict[str, str]) -> None:
    install((untracked := version_dir("1.0.0", STUB), None))

    assert launchers.check_launchers() == ["1.0.0"]

    assert os.readlink(untracked / "bin" / "hook") == SYMLINK_TARGET


def test_nothing_to_check_announces_nothing(origin: dict[str, str]) -> None:
    assert launchers.check_launchers() == []
    assert faults.drain() == []


def test_dispatch_detaches_once_per_window_and_never_from_a_spawned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(launchers, "spawn", spawned.append)
    monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
    launchers.dispatch_launcher_check()
    monkeypatch.delenv("CAPT_HOOK_SPAWNED")

    launchers.dispatch_launcher_check()
    launchers.dispatch_launcher_check()

    assert spawned == [["launchers"]]
