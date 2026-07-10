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

# Decision-capable events the cli guard exempts, and pure side-effect events it collapses.
EXEMPT_EVENTS = ["PreToolUse", "Stop", "SubagentStop", "PermissionRequest"]
GUARDED_EVENTS = ["PostToolUse", "UserPromptSubmit"]

_BASE_PAYLOAD: dict[str, dict[str, Any]] = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    "Stop": {"stop_hook_active": False},
    "SubagentStop": {"stop_hook_active": False},
    "PermissionRequest": {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    "PostToolUse": {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "done"},
    "UserPromptSubmit": {"prompt": "continue"},
}


def event_stdin(event: str, **extra: Any) -> str:
    return json.dumps({"hook_event_name": event, **_BASE_PAYLOAD[event], **extra})


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

    def test_reclaim_aborts_when_sentinel_recreated(self, sentinel_dir: Path) -> None:
        # A racing claimant freshly recreated the sentinel between our stale-stat and the
        # reclaim: re-stat sees a newer mtime, so we must not unlink its live sentinel.
        sentinel_dir.mkdir(parents=True)
        sentinel = sentinel_dir / "key"
        sentinel.write_text("")
        stale = time.time() - 100
        os.utime(sentinel, (stale, stale))
        prior = sentinel.stat()
        fresh = time.time()
        os.utime(sentinel, (fresh, fresh))
        assert once._reclaim(sentinel, prior) is False
        assert sentinel.exists()

    def test_reclaim_aborts_when_sentinel_vanished(self, sentinel_dir: Path) -> None:
        # A racing reclaimer already removed the stale sentinel: treat as a duplicate.
        sentinel_dir.mkdir(parents=True)
        sentinel = sentinel_dir / "key"
        sentinel.write_text("")
        prior = sentinel.stat()
        sentinel.unlink()
        assert once._reclaim(sentinel, prior) is False

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

    def test_symlinked_sentinel_dir_skips_guard(self, sentinel_dir: Path, tmp_path: Path) -> None:
        # A hostile symlink where the sentinel dir belongs: fail open (every call
        # dispatches), never trust it and never claim through it.
        target = tmp_path / "planted"
        target.mkdir()
        sentinel_dir.symlink_to(target, target_is_directory=True)
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload) is True
        assert once.claim_once("Stop", payload) is True
        assert list(target.iterdir()) == []


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
    @pytest.mark.parametrize("event", EXEMPT_EVENTS)
    def test_decision_events_are_exempt(self, tmp_path: Path, event: str) -> None:
        stdin = event_stdin(event)
        results = _run_pair(tmp_path, event, (stdin, stdin))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 2

    @pytest.mark.parametrize("event", GUARDED_EVENTS)
    def test_side_effect_events_collapse(self, tmp_path: Path, event: str) -> None:
        stdin = event_stdin(event)
        results = _run_pair(tmp_path, event, (stdin, stdin))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 1

    def test_concurrent_distinct_payloads_dispatch_both(self, tmp_path: Path) -> None:
        stdins = (event_stdin("UserPromptSubmit", prompt="a"), event_stdin("UserPromptSubmit", prompt="b"))
        results = _run_pair(tmp_path, "UserPromptSubmit", stdins)
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 2

    def test_ttl_zero_dispatches_both(self, tmp_path: Path) -> None:
        stdin = event_stdin("UserPromptSubmit")
        results = _run_pair(tmp_path, "UserPromptSubmit", (stdin, stdin), ttl="0")
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert _dispatch_count(tmp_path / "count") == 2

    def test_dispatches_again_after_ttl_expiry(self, tmp_path: Path) -> None:
        stdin = event_stdin("UserPromptSubmit")
        first = _run(tmp_path, "UserPromptSubmit", stdin)
        assert first.returncode == 0, first.stderr
        assert _dispatch_count(tmp_path / "count") == 1
        (sentinel,) = list((tmp_path / "tmp" / once.DIR_NAME).iterdir())
        stale = time.time() - 100
        os.utime(sentinel, (stale, stale))
        second = _run(tmp_path, "UserPromptSubmit", stdin)
        assert second.returncode == 0, second.stderr
        assert _dispatch_count(tmp_path / "count") == 2

    def test_duplicate_exits_silently(self, tmp_path: Path) -> None:
        stdin = event_stdin("UserPromptSubmit")
        first = _run(tmp_path, "UserPromptSubmit", stdin)
        assert first.returncode == 0, first.stderr
        duplicate = _run(tmp_path, "UserPromptSubmit", stdin)
        assert duplicate.returncode == 0
        assert duplicate.stdout == ""
        assert duplicate.stderr == ""
        assert _dispatch_count(tmp_path / "count") == 1
