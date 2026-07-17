"""Register a plugin pack's extra dependency marketplaces with Claude Code.

A pack-shipping Claude plugin may declare, via its ``capt-hook.toml`` ``[pack]`` manifest, further
dependency marketplaces its hooks ride on. Claude Code never registers an unknown marketplace on its
own, so a fresh install lands those dependencies unsatisfied. Discovery — the tail of a working
capt-hook dispatch — takes the union of every resolved pack's declared marketplaces and routes it
through :func:`maybe_bootstrap`, which for each not-yet-registered marketplace detaches a worker
running ``claude plugin marketplace add``. Claude Code (>=2.1.117) auto-resolves the declared plugin
dependencies from there, so no explicit ``plugin install`` is needed. The captain-hook marketplace
itself is never bootstrapped here: discovery only ever runs where the dispatcher already exists.
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
from typing import Any

from filelock import FileLock, Timeout
from loguru import logger

from captain_hook.packs import manager
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_claude_config_dir, resolve_state_dir

RETRY_COOLDOWN_SECONDS = 3600
WORKER_TIMEOUT_SECONDS = 300


def known_marketplaces_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "known_marketplaces.json"


def known_entries() -> list[tuple[str, dict[str, Any]]]:
    # Unreadable/malformed JSON or a non-object top level → [] (every repo unknown → spawn, safe).
    try:
        data = json.loads(known_marketplaces_path().read_text())
    except (OSError, ValueError):
        logger.bind(path=str(known_marketplaces_path())).debug(
            "known_marketplaces.json unreadable; no marketplaces known"
        )
        return []
    return [
        (name, entry) for name, entry in (data.items() if isinstance(data, dict) else ()) if isinstance(entry, dict)
    ]


def entry_matches(repo: str, name: str, source: object) -> bool:
    # github/git sources match precisely by repo/url (never the name), so a same-basename repo under
    # another owner isn't falsely skipped; only a repo-less source falls back to name == basename.
    match source:
        case {"source": "github", "repo": str(r)}:
            return r.casefold() == repo.casefold()
        case {"source": "github"}:
            return False
        case {"url": str(url)}:
            return repo.casefold() in url.casefold()
        case dict():
            return name.casefold() == repo.rsplit("/", 1)[-1].casefold()
        case _:
            return False


def is_known(repo: str, entries: Sequence[tuple[str, dict[str, Any]]]) -> bool:
    return any(entry_matches(repo, name, meta.get("source")) for name, meta in entries)


def bootstrap_dir() -> Path:
    return resolve_state_dir() / "bootstrap"


def bootstrap_lock_path() -> Path:
    return bootstrap_dir() / "bootstrap.lock"


def marker_path(repo: str) -> Path:
    # Keyed on (config dir, repo): known_marketplaces.json is per CLAUDE_CONFIG_DIR, so two config
    # dirs sharing a state dir must damp independently or one profile's marker suppresses another.
    key = sha256(f"{resolve_claude_config_dir().resolve()}\n{repo}".encode()).hexdigest()[:16]
    return bootstrap_dir() / f"{key}.json"


def worker_log_path() -> Path:
    return bootstrap_dir() / "marketplace.log"


def attempt_fresh(marker: Path, now: float) -> bool:
    try:
        attempted_at = json.loads(marker.read_text())["attempted_at"]
    except (OSError, ValueError, KeyError):
        return False
    # Bound both ends: a corrupt future/inf attempted_at would otherwise suppress the retry forever.
    return isinstance(attempted_at, (int, float)) and 0 <= now - attempted_at < RETRY_COOLDOWN_SECONDS


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


def maybe_bootstrap(marketplaces: Sequence[str] = ()) -> None:
    """Detach a worker to register every dependency marketplace not yet known to Claude Code.

    ``marketplaces`` is the union of the ``[pack]`` manifests' declared dependency marketplaces
    across a project's resolved packs (deduped, order-preserving); it registers exactly those and
    nothing more — the captain-hook marketplace is not implied, since discovery only ever runs where
    the dispatcher already exists. An empty union returns immediately with zero I/O, so the common
    case (no pack declares extras) costs nothing per event. Otherwise the hot path is ordered to do
    the least work: a single lock-free read of ``known_marketplaces.json`` proves every repo is
    already registered (the steady state), returning without a marker touch or a spawn. On the miss
    path a *non-blocking* FileLock serializes the re-check, marker damping, and spawn so a burst of
    concurrent events launches one worker, not N; a sibling already holding the lock returns
    immediately rather than stall dispatch. Damping is per (config dir, repo) (``bootstrap/<sha>.json``,
    an hourly cooldown), so a narrow union never suppresses a later broader one; a missing ``claude``
    binary records the attempts without spawning. The one-line notice is emitted on stderr only when a
    worker is launched — dispatch stdout carries the hook-decision JSON, so a stray line there would
    corrupt it; discovery captures this stderr for the daemon's warm replay.
    """
    if not (required := list(dict.fromkeys(marketplaces))):
        return
    known = known_entries()
    if not (missing := [r for r in required if not is_known(r, known)]):
        return
    now = time.time()
    bootstrap_dir().mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(bootstrap_lock_path()), timeout=0):
            known = known_entries()
            if not (
                to_add := [r for r in missing if not is_known(r, known) and not attempt_fresh(marker_path(r), now)]
            ):
                return
            for repo in to_add:
                record_attempt(marker_path(repo), now)
            if shutil.which("claude") is None:
                return
            spawn_worker(to_add)
            print(bootstrap_notice(to_add), file=sys.stderr)
    except Timeout:
        return


def run_bootstrap(repos: Sequence[str]) -> None:
    """Worker entry: register each still-unknown required marketplace once, under the lock.

    A sibling session may have registered a marketplace between spawn and lock acquisition, so each
    repo's known check is repeated (a fresh read) under the lock. ``claude`` is resolved to an
    absolute path so a cwd or PATH shadow can't hijack the call; a missing binary logs and exits
    cleanly. Each repo is revalidated against ``MARKETPLACE_REPO_RE`` first — the hidden
    ``pack bootstrap`` command takes repos straight from argv, bypassing load-time validation — and
    a mismatch is logged and skipped, never handed to ``marketplace add``. A failing ``marketplace
    add`` (a private or gone repo) is logged and stepped over so one bad repo can't starve the rest.
    No explicit ``plugin install``: Claude Code (>=2.1.117) auto-resolves the consumer's declared
    plugin dependencies once the marketplace is registered.
    """
    bootstrap_dir().mkdir(parents=True, exist_ok=True)
    with FileLock(str(bootstrap_lock_path())):
        if (claude := shutil.which("claude")) is None:
            logger.warning("claude binary not found on PATH; skipping marketplace bootstrap")
            return
        for repo in repos:
            if not manager.MARKETPLACE_REPO_RE.fullmatch(repo):
                logger.warning(f"skipping unvalidated marketplace repo {repo!r} in bootstrap argv")
                continue
            if is_known(repo, known_entries()):
                continue
            try:
                subprocess.run(
                    [claude, "plugin", "marketplace", "add", repo], check=True, timeout=WORKER_TIMEOUT_SECONDS
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.opt(exception=True).warning(f"marketplace add failed for {repo!r}; continuing to the next repo")
