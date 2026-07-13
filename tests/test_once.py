from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from captain_hook import once
from captain_hook.types import Event
from captain_hook.util.paths import resolve_cache_dir
from tests.helpers import run_cli

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "once_hooks"
CACHE_NAMESPACE = resolve_cache_dir().name

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
    # once resolves its dir under resolve_cache_dir() ($XDG_CACHE_HOME/captain-hook); pin
    # XDG_CACHE_HOME to a per-test tmp so no sentinel ever touches the real ~/.cache.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return resolve_cache_dir() / once.DIR_NAME


class TestClaimOnce:
    def test_first_wins_second_collapses(self, sentinel_dir: Path) -> None:
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload, async_=False) is True
        assert once.claim_once("Stop", payload, async_=False) is False

    def test_distinct_payloads_both_win(self, sentinel_dir: Path) -> None:
        assert once.claim_once("Stop", b'{"a": 1}', async_=False) is True
        assert once.claim_once("Stop", b'{"a": 2}', async_=False) is True

    def test_distinct_events_same_payload_both_win(self, sentinel_dir: Path) -> None:
        payload = b'{"stop_hook_active": false}'
        assert once.claim_once("Stop", payload, async_=False) is True
        assert once.claim_once("SubagentStop", payload, async_=False) is True

    def test_sync_and_async_variants_both_win(self, sentinel_dir: Path) -> None:
        # One event fans out into a sync pass and an async pass that dispatch disjoint hook
        # sets; byte-identical stdin must not let one variant's token swallow the other. Each
        # variant claims independently, and a duplicate within a variant still collapses.
        payload = b'{"reason": "clear"}'
        assert once.claim_once("SessionEnd", payload, async_=False) is True
        assert once.claim_once("SessionEnd", payload, async_=True) is True
        assert once.claim_once("SessionEnd", payload, async_=False) is False
        assert once.claim_once("SessionEnd", payload, async_=True) is False

    def test_ttl_zero_disables_guard(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(once.TTL_ENV, "0")
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload, async_=False) is True
        assert once.claim_once("Stop", payload, async_=False) is True
        assert not sentinel_dir.exists()

    def test_stale_sentinel_reclaimed(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(once.TTL_ENV, "10")
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload, async_=False) is True
        assert once.claim_once("Stop", payload, async_=False) is False
        (sentinel,) = list(sentinel_dir.iterdir())
        stale = time.time() - 100
        os.utime(sentinel, (stale, stale))
        assert once.claim_once("Stop", payload, async_=False) is True

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
        once.claim_once("Stop", b'{"old": 1}', async_=False)
        (ancient,) = list(sentinel_dir.iterdir())
        stamp = time.time() - 1000
        os.utime(ancient, (stamp, stamp))
        once.claim_once("Stop", b'{"new": 1}', async_=False)
        remaining = list(sentinel_dir.iterdir())
        assert ancient.name not in {p.name for p in remaining}
        assert len(remaining) == 1

    def test_symlinked_sentinel_dir_skips_guard(self, sentinel_dir: Path, tmp_path: Path) -> None:
        # A hostile symlink where the sentinel dir belongs: fail open (every call
        # dispatches), never trust it and never claim through it.
        target = tmp_path / "planted"
        target.mkdir()
        sentinel_dir.parent.mkdir(parents=True, exist_ok=True)
        sentinel_dir.symlink_to(target, target_is_directory=True)
        payload = b'{"a": 1}'
        assert once.claim_once("Stop", payload, async_=False) is True
        assert once.claim_once("Stop", payload, async_=False) is True
        assert list(target.iterdir()) == []


class TestOnceGuard:
    def test_winner_dispatches_duplicate_collapses(self, sentinel_dir: Path) -> None:
        payload = b'{"a": 1}'
        with once.once_guard(Event.PostToolUse, "PostToolUse", payload, async_=False) as dispatch_now:
            assert dispatch_now is True  # first sibling wins the claim and dispatches
        with once.once_guard(Event.PostToolUse, "PostToolUse", payload, async_=False) as dispatch_now:
            assert dispatch_now is False  # a still-fresh sibling collapses silently

    def test_decision_event_never_collapses_and_claims_nothing(self, sentinel_dir: Path) -> None:
        # Decision-capable events are exempt: swallowing a sibling could bypass a gate. They
        # always dispatch and claim no sentinel, so repeats never collapse.
        payload = b'{"stop_hook_active": false}'
        for _ in range(2):
            with once.once_guard(Event.Stop, "Stop", payload, async_=False) as dispatch_now:
                assert dispatch_now is True
        assert not sentinel_dir.exists()  # exempt path never touches the sentinel dir

    def test_release_on_dispatch_failure_frees_sibling(self, sentinel_dir: Path) -> None:
        # The winning claimer's dispatch raises: its sentinel must be released so a slower
        # healthy sibling can re-claim and dispatch, rather than the event being lost for the
        # whole TTL. Without release_once, the follow-up claim_once would collapse to False.
        payload = b'{"a": 1}'
        with pytest.raises(RuntimeError), once.once_guard(Event.PostToolUse, "PostToolUse", payload, async_=False):
            raise RuntimeError("dispatch blew up")
        assert once.claim_once("PostToolUse", payload, async_=False) is True

    def test_release_once_is_noop_when_unclaimed(self, sentinel_dir: Path) -> None:
        # A guarded event whose dispatch never claimed (or was already reaped): releasing a
        # sentinel that isn't there must not raise.
        once.release_once("PostToolUse", b'{"a": 1}', async_=False)


def _dispatch_count(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _sentinel_root(cache_home: Path) -> Path:
    return cache_home / CACHE_NAMESPACE / once.DIR_NAME


def _run(
    tmp_path: Path,
    event: str,
    stdin: str,
    *,
    ttl: str | None = None,
    tmpdir: Path | None = None,
) -> Any:
    (cache_home := tmp_path / "cache").mkdir(exist_ok=True)
    (td := tmpdir or tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    env = {
        "TMPDIR": str(td),
        "XDG_CACHE_HOME": str(cache_home),
        "CAPT_HOOK_TEST_COUNTER": str(tmp_path / "count"),
    }
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
        (sentinel,) = list(_sentinel_root(tmp_path / "cache").iterdir())
        stale = time.time() - 100
        os.utime(sentinel, (stale, stale))
        second = _run(tmp_path, "UserPromptSubmit", stdin)
        assert second.returncode == 0, second.stderr
        assert _dispatch_count(tmp_path / "count") == 2

    def test_divergent_tmpdir_same_cache_dedupes(self, tmp_path: Path) -> None:
        # Siblings with divergent TMPDIR but one XDG_CACHE_HOME share the sentinel and dedupe.
        stdin = event_stdin("UserPromptSubmit")
        first = _run(tmp_path, "UserPromptSubmit", stdin, tmpdir=tmp_path / "tmp-a")
        assert first.returncode == 0, first.stderr
        assert _dispatch_count(tmp_path / "count") == 1
        second = _run(tmp_path, "UserPromptSubmit", stdin, tmpdir=tmp_path / "tmp-b")
        assert second.returncode == 0, second.stderr
        assert _dispatch_count(tmp_path / "count") == 1

    def test_duplicate_exits_silently(self, tmp_path: Path) -> None:
        stdin = event_stdin("UserPromptSubmit")
        first = _run(tmp_path, "UserPromptSubmit", stdin)
        assert first.returncode == 0, first.stderr
        duplicate = _run(tmp_path, "UserPromptSubmit", stdin)
        assert duplicate.returncode == 0
        assert duplicate.stdout == ""
        assert duplicate.stderr == ""
        assert _dispatch_count(tmp_path / "count") == 1
