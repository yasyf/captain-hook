from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from captain_hook.utils import resolve_binary


def make_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_plain(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not executable\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def test_plugin_root_bin_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_root = tmp_path / "plugin"
    winner = make_exe(plugin_root / "bin" / "tool")
    extra = tmp_path / "extra"
    make_exe(extra / "tool")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tool")

    assert resolve_binary("tool", extra_dirs=[extra]) == str(winner)


def test_extra_dirs_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tool")
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    winner = make_exe(second / "tool")

    assert resolve_binary("tool", extra_dirs=[first, second]) == str(winner)


def test_which_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tool")

    assert resolve_binary("tool", extra_dirs=[tmp_path]) == "/usr/bin/tool"


def test_non_executable_file_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_root = tmp_path / "plugin"
    make_plain(plugin_root / "bin" / "tool")
    extra = tmp_path / "extra"
    winner = make_exe(extra / "tool")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tool")

    assert resolve_binary("tool", extra_dirs=[extra]) == str(winner)


def test_nothing_found_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert resolve_binary("tool", extra_dirs=[tmp_path]) is None


def test_directory_named_like_binary_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    candidate = tmp_path / "tool"
    candidate.mkdir()
    candidate.chmod(candidate.stat().st_mode | os.X_OK)

    assert resolve_binary("tool", extra_dirs=[tmp_path]) is None
