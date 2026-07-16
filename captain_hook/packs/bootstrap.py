"""Marketplace self-bootstrap for consumer plugins.

A pack-shipping Claude plugin declares captain-hook — and, via its ``capt-hook.toml`` manifest, any
further dependency marketplaces its hooks ride on — as dependencies, but Claude Code never registers
an unknown marketplace on its own, so a fresh install lands the consumer disabled with
``dependency-unsatisfied``. The consumer's one shipped hook runs ``pack attach`` from PyPI on every
SessionStart regardless of whether the captain-hook plugin is installed; that call routes through
:func:`maybe_bootstrap`, which — for every required marketplace not yet registered — detaches a
worker that runs ``claude plugin marketplace add``. Claude Code (>=2.1.117) auto-resolves the
consumer's declared plugin dependencies from there, so no explicit ``plugin install`` is needed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from filelock import FileLock
from loguru import logger

from captain_hook.packs import manager
from captain_hook.packs.contract import MARKETPLACE_REPO
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_claude_config_dir, resolve_state_dir

RETRY_COOLDOWN_SECONDS = 3600
WORKER_TIMEOUT_SECONDS = 300


def known_marketplaces_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "known_marketplaces.json"


def known_repos() -> set[str]:
    try:
        data = json.loads(known_marketplaces_path().read_text())
    except (OSError, ValueError):
        logger.bind(path=str(known_marketplaces_path())).debug(
            "known_marketplaces.json unreadable; no marketplaces known"
        )
        return set()
    return {
        repo
        for entry in (data.values() if isinstance(data, dict) else ())
        if isinstance(entry, dict)
        and isinstance(source := entry.get("source"), dict)
        and source.get("source") == "github"
        and isinstance(repo := source.get("repo"), str)
    }


def bootstrap_dir() -> Path:
    return resolve_state_dir() / "bootstrap"


def bootstrap_lock_path() -> Path:
    return bootstrap_dir() / "bootstrap.lock"


def marker_path(repo: str) -> Path:
    return bootstrap_dir() / f"{sha256(repo.encode()).hexdigest()[:16]}.json"


def worker_log_path() -> Path:
    return bootstrap_dir() / "marketplace.log"


def attempt_fresh(marker: Path, now: float) -> bool:
    try:
        attempted_at = json.loads(marker.read_text())["attempted_at"]
    except (OSError, ValueError, KeyError):
        return False
    return isinstance(attempted_at, (int, float)) and now - attempted_at < RETRY_COOLDOWN_SECONDS


def record_attempt(marker: Path, now: float) -> None:
    manager.atomic_write(marker, json.dumps({"attempted_at": now}))


def bootstrap_notice(repos: Sequence[str]) -> str:
    return (
        f"capt-hook: registering the plugin marketplace(s) {', '.join(repos)} in the background — this "
        "plugin's hooks depend on them. Takes effect via Claude Code's background plugin auto-update, "
        "/reload-plugins, or the next session."
    )


def spawn_worker(repos: Sequence[str]) -> None:
    (log_path := worker_log_path()).parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-I", "-m", "captain_hook", "pack", "bootstrap", *repos],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=reqenv.cwd(),
            env=reqenv.env_map(),
        )


def maybe_bootstrap(extra_marketplaces: Sequence[str] = ()) -> str | None:
    """Detach a worker to register every required marketplace not yet known to Claude Code.

    captain-hook's own marketplace is always required; ``extra_marketplaces`` are the consumer
    manifest's declared dependency marketplaces. Hot-path ordered to do the least work: a single
    lock-free read of ``known_marketplaces.json`` proves every required repo is registered (the
    steady state), returning without a marker touch or a spawn. On the miss path a single FileLock
    serializes the re-check, marker damping, and spawn so a burst of concurrent sessions launches
    one worker, not N. Damping is per repo (``bootstrap/<sha(repo)>.json``, an hourly cooldown), so
    a narrow attach never suppresses a later broader one; a missing ``claude`` binary records the
    attempts without spawning. Returns the one-line notice only when a worker is launched — that
    line is the sole deliberate SessionStart stdout, landing in the model context.
    """
    required = [MARKETPLACE_REPO, *extra_marketplaces]
    if not (missing := [r for r in required if r not in known_repos()]):
        return None
    now = time.time()
    bootstrap_dir().mkdir(parents=True, exist_ok=True)
    with FileLock(str(bootstrap_lock_path())):
        known = known_repos()
        if not (to_add := [r for r in missing if r not in known and not attempt_fresh(marker_path(r), now)]):
            return None
        for repo in to_add:
            record_attempt(marker_path(repo), now)
        if shutil.which("claude") is None:
            return None
        spawn_worker(to_add)
        return bootstrap_notice(to_add)


def run_bootstrap(repos: Sequence[str]) -> None:
    """Worker entry: register each still-unknown required marketplace once, under the lock.

    A sibling session may have registered a marketplace between spawn and lock acquisition, so each
    repo's known check is repeated under the lock. ``claude`` is resolved to an absolute path so a
    cwd or PATH shadow can't hijack the call; a missing binary logs and exits cleanly. Each
    ``marketplace add`` runs with ``check=True`` — a failure raises and lands loud in the worker
    log. No explicit ``plugin install``: Claude Code (>=2.1.117) auto-resolves the consumer's
    declared plugin dependencies once the marketplace is registered.
    """
    bootstrap_dir().mkdir(parents=True, exist_ok=True)
    with FileLock(str(bootstrap_lock_path())):
        if (claude := shutil.which("claude")) is None:
            logger.warning("claude binary not found on PATH; skipping marketplace bootstrap")
            return
        for repo in repos:
            if repo not in known_repos():
                subprocess.run(
                    [claude, "plugin", "marketplace", "add", repo], check=True, timeout=WORKER_TIMEOUT_SECONDS
                )
