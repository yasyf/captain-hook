from __future__ import annotations

import importlib.metadata
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.daemon import lifecycle
from captain_hook.daemon.lifecycle import (
    DEFAULT_IDLE_S,
    Watchdog,
    build_id,
    drop_caches,
    dump_stacks,
    format_stacks,
    idle_expired,
    idle_limit,
    is_checkout,
    should_restart,
    source_digest,
)


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        (path := root / rel).parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


class TestSourceDigest:
    def test_stable_across_reads(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "pkg", {"a.py": "x = 1\n", "sub/b.py": "y = 2\n"})
        assert source_digest([root]) == source_digest([root])

    def test_content_change_changes_digest(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "pkg", {"a.py": "x = 1\n"})
        before = source_digest([root])
        (root / "a.py").write_text("x = 1000\n")
        assert source_digest([root]) != before

    def test_mtime_change_changes_digest(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "pkg", {"a.py": "x = 1\n"})
        before = source_digest([root])
        os.utime(root / "a.py", ns=(0, 0))
        assert source_digest([root]) != before

    def test_preserved_mtime_content_rewrite_changes_digest(self, tmp_path: Path) -> None:
        # A same-size, mtime-preserved rewrite (touch -r style) only moves ctime; the editable-checkout
        # watchdog must still notice it, so ctime is part of the digest tuple.
        root = make_tree(tmp_path / "pkg", {"a.py": "x = 1\n"})
        before = source_digest([root])
        st = (root / "a.py").stat()
        (root / "a.py").write_text("y = 2\n")  # same byte length, different content
        os.utime(root / "a.py", ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
        after = (root / "a.py").stat()
        assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns
        assert source_digest([root]) != before

    def test_add_and_remove_change_digest(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "pkg", {"a.py": "x = 1\n"})
        before = source_digest([root])
        (root / "c.py").write_text("z = 3\n")
        added = source_digest([root])
        assert added != before
        (root / "c.py").unlink()
        assert source_digest([root]) == before

    def test_non_python_files_ignored(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "pkg", {"a.py": "x = 1\n"})
        before = source_digest([root])
        (root / "notes.txt").write_text("hello")
        assert source_digest([root]) == before

    def test_spans_multiple_roots(self, tmp_path: Path) -> None:
        one = make_tree(tmp_path / "one", {"a.py": "x = 1\n"})
        two = make_tree(tmp_path / "two", {"b.py": "y = 2\n"})
        assert source_digest([one, two]) != source_digest([one])


class TestBuildId:
    def test_is_checkout_detection(self) -> None:
        assert is_checkout(Path("/home/me/proj/captain_hook")) is True
        assert is_checkout(Path("/venv/lib/python3.13/site-packages/captain_hook")) is False
        assert is_checkout(Path("/usr/lib/python3/dist-packages/captain_hook")) is False

    def test_build_id_matches_checkout_state(self) -> None:
        build_id.cache_clear()
        bid = build_id()
        version = importlib.metadata.version(lifecycle.DIST_NAME)
        if is_checkout(lifecycle.source_roots()[0]):
            assert bid == f"{version}+{source_digest(lifecycle.source_roots())}"
        else:
            assert bid == version

    def test_build_id_is_throttled_within_the_window(self) -> None:
        build_id.cache_clear()
        assert build_id() == build_id()


class TestShouldRestart:
    @pytest.mark.parametrize(
        ("recorded", "current", "consecutive", "expected"),
        [
            pytest.param("A", "A", 0, (0, False), id="match_resets"),
            pytest.param("A", "A", 1, (0, False), id="match_after_one_mismatch_resets"),
            pytest.param("A", "B", 0, (1, False), id="first_mismatch_waits"),
            pytest.param("A", "B", 1, (2, True), id="second_consecutive_mismatch_restarts"),
        ],
    )
    def test_debounce(self, recorded: str, current: str, consecutive: int, expected: tuple[int, bool]) -> None:
        assert should_restart(recorded, current, consecutive) == expected


class TestIdle:
    def test_default_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOOKS_DAEMON_IDLE_S", raising=False)
        assert idle_limit() == DEFAULT_IDLE_S

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_DAEMON_IDLE_S", "30")
        assert idle_limit() == 30.0

    @pytest.mark.parametrize(
        ("last", "now", "limit", "expired"),
        [
            pytest.param(100.0, 150.0, 120.0, False, id="within_window"),
            pytest.param(0.0, 120.0, 120.0, True, id="exactly_at_limit"),
            pytest.param(0.0, 500.0, 120.0, True, id="past_limit"),
        ],
    )
    def test_idle_expired(self, last: float, now: float, limit: float, expired: bool) -> None:
        assert idle_expired(last, now, limit) is expired


class TestDropCaches:
    def test_clears_every_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.daemon import transcache
        from captain_hook.review.repo import resolve_repo_key
        from captain_hook.signals.nlp import parse
        from captain_hook.util.http import github_token

        seen: list[str] = []
        registry = MagicMock()
        monkeypatch.setattr(transcache, "cache_clear", lambda: seen.append("transcache"))
        monkeypatch.setattr(github_token, "cache_clear", lambda: seen.append("github_token"))
        monkeypatch.setattr(resolve_repo_key, "cache_clear", lambda: seen.append("resolve_repo_key"))
        monkeypatch.setattr(parse, "cache_clear", lambda: seen.append("nlp"))

        drop_caches(registry)

        assert registry.drop_all.called
        assert set(seen) == {"transcache", "github_token", "resolve_repo_key", "nlp"}


class TestStacksAndWatchdog:
    def test_format_stacks_names_current_thread(self) -> None:
        text = format_stacks()
        assert "# thread MainThread" in text
        assert "format_stacks" in text

    def test_dump_stacks_logs_without_brace_errors(self, logcap: Any) -> None:
        dump_stacks()
        assert any("SIGUSR1 stack dump" in record.message for record in logcap.records)

    def test_watchdog_restarts_after_two_mismatched_ticks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "build_id", lambda: "changed")
        fired = threading.Event()
        watchdog = Watchdog("original", fired.set, interval=0.005)
        watchdog.start()
        try:
            assert fired.wait(2.0)
        finally:
            watchdog.stop()

    def test_watchdog_stable_build_never_restarts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "build_id", lambda: "same")
        fired = threading.Event()
        watchdog = Watchdog("same", fired.set, interval=0.005)
        watchdog.start()
        try:
            assert not fired.wait(0.1)
        finally:
            watchdog.stop()
