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

import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from filelock import Timeout
from loguru import logger
from pydantic import Field

from captain_hook.durable import DurableState, DurableStore, durable_dir
from captain_hook.packs import manager
from captain_hook.util import reqenv
from captain_hook.util.fs import read_json
from captain_hook.util.paths import resolve_claude_config_dir, resolve_state_dir

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.durable import DurableSlot

RETRY_COOLDOWN_SECONDS = 3600
WORKER_TIMEOUT_SECONDS = 300


class BootstrapState(DurableState, scope="global"):
    """Cross-session marketplace-bootstrap damping: the last-attempt epoch keyed by (config dir, repo)."""

    attempts: dict[str, float] = Field(default_factory=dict)


def known_marketplaces_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "known_marketplaces.json"


def known_entries() -> list[tuple[str, dict[str, Any]]]:
    # Unreadable/malformed JSON or a non-object top level → [] (every repo unknown → spawn, safe).
    return [
        (name, entry) for name, entry in read_json(known_marketplaces_path(), {}).items() if isinstance(entry, dict)
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


def worker_log_path() -> Path:
    return bootstrap_dir() / "marketplace.log"


def attempt_key(repo: str) -> str:
    # Keyed on (config dir, repo): known_marketplaces.json is per CLAUDE_CONFIG_DIR, so two config
    # dirs sharing a state dir must damp independently or one profile's attempt suppresses another.
    return sha256(f"{resolve_claude_config_dir().resolve()}\n{repo}".encode()).hexdigest()[:16]


def attempt_fresh(attempts: dict[str, float], key: str, now: float) -> bool:
    # Bound both ends: a corrupt future/inf timestamp would otherwise suppress the retry forever.
    return (ts := attempts.get(key)) is not None and 0 <= now - ts < RETRY_COOLDOWN_SECONDS


def bootstrap_slot() -> DurableSlot[BootstrapState]:
    """The global, filelock-guarded slot holding the cross-session bootstrap damping state."""
    return DurableStore(durable_dir("global", None))[BootstrapState]


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
    already registered (the steady state), returning without touching the damping state or spawning.
    On the miss path the durable :class:`BootstrapState`'s *non-blocking* lock — a
    ``mutate(timeout=0)`` acquire — serializes the re-check and damping; the attempt is persisted
    when the locked block exits, *before* the worker spawns, so a spawn failure can't un-record it.
    A burst of concurrent events launches one worker, not N — a sibling that acquires the lock after
    the attempt lands sees it under cooldown and skips, and a sibling that can't acquire returns
    immediately rather than stall dispatch. Damping is per (config dir, repo) (an ``attempts`` map
    keyed by ``sha(config dir, repo)``, an hourly cooldown), so a narrow union never suppresses a
    later broader one; a missing ``claude`` binary records the attempts without spawning. A spawn
    logs one loguru info line and prints nothing on any stream — dispatch stdout carries the
    hook-decision JSON, and an event-like stderr notice would replay stale on the daemon's warm
    cache hits.
    """
    if not (required := list(dict.fromkeys(marketplaces))):
        return
    known = known_entries()
    if not (missing := [r for r in required if not is_known(r, known)]):
        return
    now = time.time()
    try:
        with bootstrap_slot().mutate(timeout=0) as state:
            known = known_entries()
            if not (
                to_add := [
                    r
                    for r in missing
                    if not is_known(r, known) and not attempt_fresh(state.attempts, attempt_key(r), now)
                ]
            ):
                return
            for repo in to_add:
                state.attempts[attempt_key(repo)] = now
    except Timeout:
        return
    # Spawn after the locked block persists the attempt, so a spawn failure can't un-record it.
    if shutil.which("claude") is None:
        return
    spawn_worker(to_add)
    logger.bind(marketplaces=to_add).info("registering plugin dependency marketplace(s) in the background")


def run_bootstrap(repos: Sequence[str]) -> None:
    """Worker entry: register each still-unknown required marketplace once, under the lock.

    A sibling session may have registered a marketplace between spawn and lock acquisition, so each
    repo's known check is repeated (a fresh read) under the durable :class:`BootstrapState` lock —
    the same lock :func:`maybe_bootstrap` acquires, so a worker in flight makes new dispatches skip.
    ``claude`` is resolved to an absolute path so a cwd or PATH shadow can't hijack the call; a
    missing binary logs and exits cleanly. Each repo is revalidated against
    ``MARKETPLACE_REPO_RE`` first — the hidden ``pack bootstrap`` command takes repos straight from
    argv, bypassing load-time validation — and a mismatch is logged and skipped, never handed to
    ``marketplace add``. A failing ``marketplace add`` (a private or gone repo) is logged and
    stepped over so one bad repo can't starve the rest. No explicit ``plugin install``: Claude Code
    (>=2.1.117) auto-resolves the consumer's declared plugin dependencies once the marketplace is
    registered.
    """
    with bootstrap_slot().mutate():
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
