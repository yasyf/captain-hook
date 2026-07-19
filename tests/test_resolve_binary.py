from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from captain_hook.util import resolve_binary
from captain_hook.util.fs import atomic_write, binary_supports


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


@pytest.fixture(autouse=True)
def _clear_binary_supports_cache() -> None:
    binary_supports.cache_clear()


def probe(stdout: str = "", stderr: str = "") -> object:
    return lambda *args, **kwargs: SimpleNamespace(stdout=stdout, stderr=stderr)


def test_binary_supports_true_when_flag_in_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: "/usr/bin/tool")
    monkeypatch.setattr("captain_hook.util.fs.subprocess.run", probe(stdout="usage: tool [--foo] [--bar]"))
    assert binary_supports("tool", "--foo") is True
    assert binary_supports("tool", "--nope") is False


def test_binary_supports_reads_stderr_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: "/usr/bin/tool")
    monkeypatch.setattr("captain_hook.util.fs.subprocess.run", probe(stderr="options: --quiet --verbose"))
    assert binary_supports("tool", "--verbose") is True


def test_binary_supports_false_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: None)
    assert binary_supports("tool", "--foo") is False


def test_binary_supports_false_on_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: "/usr/bin/tool")

    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("cannot exec")

    monkeypatch.setattr("captain_hook.util.fs.subprocess.run", boom)
    assert binary_supports("tool", "--foo") is False


def test_binary_supports_memoized_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def run(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return SimpleNamespace(stdout="--foo", stderr="")

    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: "/usr/bin/tool")
    monkeypatch.setattr("captain_hook.util.fs.subprocess.run", run)
    assert binary_supports("tool", "--foo") is True
    assert binary_supports("tool", "--foo") is True
    assert len(calls) == 1  # second call served from the ttl cache, no re-probe


def test_atomic_write_creates_parents_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "nested" / "f.json"
    atomic_write(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'
    assert list(target.parent.iterdir()) == [target]  # no leftover temp file
    atomic_write(target, '{"a": 2}')
    assert target.read_text() == '{"a": 2}'
    assert list(target.parent.iterdir()) == [target]


def test_session_slot_set_delegates_to_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import captain_hook.session as session_mod

    class Model(BaseModel):
        value: int = 0

    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(session_mod, "atomic_write", lambda path, text: calls.append((path, text)))
    slot = session_mod.SessionSlot(tmp_path, Model)
    slot.set(Model(value=7))
    assert len(calls) == 1
    path, text = calls[0]
    assert path == slot.path
    assert '"value":7' in text.replace(" ", "")


def test_atomic_write_round_trips_non_ascii(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    payload = '{"greeting": "héllo — 世界 🌍"}'
    atomic_write(target, payload)
    assert target.read_text(encoding="utf-8") == payload


def test_binary_supports_survives_non_utf8_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A binary whose --help emits invalid UTF-8 must degrade (errors="replace"), not raise
    # UnicodeDecodeError past the OSError/SubprocessError guard.
    stub = tmp_path / "stub"
    stub.write_text("#!/bin/sh\nprintf '\\377\\376 --foo\\n'\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr("captain_hook.util.fs.resolve_binary", lambda name: str(stub))
    assert binary_supports("stub", "--foo") is True
