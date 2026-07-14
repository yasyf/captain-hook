"""Marketplace self-bootstrap for consumer plugins.

A pack-shipping Claude plugin declares captain-hook as a dependency, but Claude Code never
registers an unknown marketplace on its own, so a fresh install lands the consumer disabled with
``dependency-unsatisfied``. The consumer's one shipped hook runs ``pack attach`` from PyPI on
every SessionStart regardless of whether the captain-hook plugin is installed; that call routes
through :func:`maybe_bootstrap`, which — when the captain-hook marketplace isn't registered —
detaches a worker that runs ``claude plugin marketplace add`` + ``claude plugin install``. Claude
Code's background plugin auto-update resolves the dependency from there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

from filelock import FileLock
from loguru import logger

from captain_hook.packs import manager
from captain_hook.packs.contract import MARKETPLACE_NAME, MARKETPLACE_REPO, PLUGIN_ID
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_claude_config_dir, resolve_state_dir

RETRY_COOLDOWN_SECONDS = 3600
WORKER_TIMEOUT_SECONDS = 300
BOOTSTRAP_NOTICE = (
    "capt-hook: registering the captain-hook plugin marketplace and installing its plugin in the "
    "background — this plugin's hooks depend on it. Takes effect via Claude Code's background plugin "
    "auto-update, /reload-plugins, or the next session."
)


def known_marketplaces_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "known_marketplaces.json"


def marketplace_known() -> bool:
    try:
        data = json.loads(known_marketplaces_path().read_text())
    except (OSError, ValueError):
        logger.bind(path=str(known_marketplaces_path())).debug(
            "known_marketplaces.json unreadable; marketplace unknown"
        )
        return False
    return isinstance(data, dict) and MARKETPLACE_NAME in data


def bootstrap_dir() -> Path:
    return resolve_state_dir() / "bootstrap"


def marker_path() -> Path:
    key = sha256(str(resolve_claude_config_dir().resolve()).encode()).hexdigest()[:16]
    return bootstrap_dir() / f"{key}.json"


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


def spawn_worker() -> None:
    (log_path := worker_log_path()).parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-I", "-m", "captain_hook", "pack", "bootstrap"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=reqenv.cwd(),
            env=reqenv.env_map(),
        )


def maybe_bootstrap() -> str | None:
    """Detach the marketplace-registration worker when the captain-hook marketplace is unknown.

    Hot-path ordered to do the least work: a single lock-free read proves the marketplace is
    already registered (the steady state). On the miss path a FileLock serializes the re-check,
    marker damping, and spawn so a burst of concurrent sessions launches one worker, not N; an
    hourly marker damps re-spawn churn and a missing ``claude`` binary records the attempt without
    spawning. Returns the one-line notice only when a worker is launched — that line is the sole
    deliberate SessionStart stdout, landing in the model context.
    """
    if marketplace_known():
        return None
    now = time.time()
    marker = marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(marker.with_name(marker.name + ".lock"))):
        if marketplace_known() or attempt_fresh(marker, now):
            return None
        record_attempt(marker, now)
        if shutil.which("claude") is None:
            return None
        spawn_worker()
        return BOOTSTRAP_NOTICE


def run_bootstrap() -> None:
    """Worker entry: register the captain-hook marketplace and install its plugin, once per config dir.

    A sibling session may have registered the marketplace between spawn and lock acquisition, so the
    known check is repeated under the lock. ``claude`` is resolved to an absolute path so a cwd or
    PATH shadow can't hijack the install; a missing binary logs and exits cleanly. Both calls run
    with ``check=True`` — a failure raises and lands loud in the worker log.
    """
    marker = marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(marker.with_name(marker.name + ".lock"))):
        if marketplace_known():
            return
        if (claude := shutil.which("claude")) is None:
            logger.warning("claude binary not found on PATH; skipping marketplace bootstrap")
            return
        subprocess.run(
            [claude, "plugin", "marketplace", "add", MARKETPLACE_REPO], check=True, timeout=WORKER_TIMEOUT_SECONDS
        )
        subprocess.run([claude, "plugin", "install", PLUGIN_ID], check=True, timeout=WORKER_TIMEOUT_SECONDS)
