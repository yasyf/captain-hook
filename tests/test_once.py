from __future__ import annotations

import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from captain_hook import once
from tests.helpers import run_cli

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "once_hooks"


def stop_stdin(**extra: Any) -> str:
    return json.dumps({"hook_event_name": "Stop", "stop_hook_active": False, **extra})


@pytest.fixture
def sentinel_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # once resolves its dir via tempfile.gettempdir(); tempfile caches the tmpdir, so
    # override it directly rather than via the (already-cached) TMPDIR env in-process.
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root / once.DIR_NAME


class TestClaimOnce:
    def test_first_wins_second_collapses(self, sentinel_dir: Path) -> None:
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload) is True
        assert once.claim_once("Stop", payload) is False

    def test_distinct_payloads_both_win(self, sentinel_dir: Path) -> None:
        assert once.claim_once("Stop", b'{"a": 1}') is True
        assert once.claim_once("Stop", b'{"a": 2}') is True

    def test_distinct_events_same_payload_both_win(self, sentinel_dir: Path) -> None:
        payload = b'{"stop_hook_active": false}'
        assert once.claim_once("Stop", payload) is True
        assert once.claim_once("SubagentStop", payload) is True

    def test_ttl_zero_disables_guard(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(once.TTL_ENV, "0")
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload) is True
        assert once.claim_once("Stop", payload) is True
        assert not sentinel_dir.exists()

    def test_stale_sentinel_reclaimed(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(once.TTL_ENV, "10")
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload) is True
        assert once.claim_once("Stop", payload) is False
        (sentinel,) = list(sentinel_dir.iterdir())
        stale = time.time() - 100
        os.utime(sentinel, (stale, stale))
        assert once.claim_once("Stop", payload) is True

    def test_reap_removes_ancient_sentinels(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(once.TTL_ENV, "10")
        once.claim_once("Stop", b'{"old": 1}')
        (ancient,) = list(sentinel_dir.iterdir())
        stamp = time.time() - 1000
        os.utime(ancient, (stamp, stamp))
        once.claim_once("Stop", b'{"new": 1}')
        remaining = list(sentinel_dir.iterdir())
        assert ancient.name not in {p.name for p in remaining}
        assert len(remaining) == 1


def _dispatch_count(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _run(
    tmp_path: Path,
    event: str,
    stdin: str,
    *,
    ttl: str | None = None,
) -> Any:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env = {"TMPDIR": str(tmpdir), "CAPT_HOOK_TEST_COUNTER": str(tmp_path / "count")}
    if ttl is not None:
        env[once.TTL_ENV] = ttl
    return run_cli("run", event, hooks_dir=str(FIXTURES_DIR), root_dir=str(tmp_path), stdin_data=stdin, env=env)


def _run_pair(tmp_path: Path, event: str, stdins: tuple[str, str], *, ttl: str | None = None) -> list[Any]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run, tmp_path, event, s, ttl=ttl) for s in stdins]
        return [f.result() for f in futures]


class TestGuardEndToEnd:
    def test_concurrent_identical_dispatches_once(self, tmp_path: Path) -> None:
        stdin = stop_stdin()
        results = _run_pair(tmp_path, "Stop", (stdin, stdin))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 1

    def test_concurrent_distinct_payloads_dispatch_both(self, tmp_path: Path) -> None:
        results = _run_pair(tmp_path, "Stop", (stop_stdin(nonce="a"), stop_stdin(nonce="b")))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 2

    def test_ttl_zero_dispatches_both(self, tmp_path: Path) -> None:
        stdin = stop_stdin()
        results = _run_pair(tmp_path, "Stop", (stdin, stdin), ttl="0")
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 2

    def test_dispatches_again_after_ttl_expiry(self, tmp_path: Path) -> None:
        stdin = stop_stdin()
        first = _run(tmp_path, "Stop", stdin)
        assert first.returncode == 0, first.stderr
        assert _dispatch_count(tmp_path / "count") == 1
        (sentinel,) = list((tmp_path / "tmp" / once.DIR_NAME).iterdir())
        stale = time.time() - 100
        os.utime(sentinel, (stale, stale))
        second = _run(tmp_path, "Stop", stdin)
        assert second.returncode == 0, second.stderr
        assert _dispatch_count(tmp_path / "count") == 2

    def test_duplicate_exits_silently(self, tmp_path: Path) -> None:
        stdin = stop_stdin()
        first = _run(tmp_path, "Stop", stdin)
        assert first.returncode == 0, first.stderr
        duplicate = _run(tmp_path, "Stop", stdin)
        assert duplicate.returncode == 0
        assert duplicate.stdout == ""
        assert duplicate.stderr == ""
        assert _dispatch_count(tmp_path / "count") == 1

    def test_pretooluse_is_exempt(self, tmp_path: Path) -> None:
        stdin = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        results = _run_pair(tmp_path, "PreToolUse", (stdin, stdin))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 2
