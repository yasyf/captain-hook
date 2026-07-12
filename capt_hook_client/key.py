"""Shared worker-identity and on-disk-path contract for the resident daemon.

Both the thin client and the daemon import this module to agree, byte-for-byte, on which
worker serves a project, where its socket/lock/meta/log files live, and how a build is
fingerprinted. It is stdlib-only and cheap to import (microseconds) so the daemon pays
nothing to resolve a request's worker.

The worker-key env subset is deliberately ADDITIVE: only the keys in
:data:`WORKER_ENV_KEYS` (plus every ``HOOKS_*`` var) partition workers. Per-session vars
such as ``CLAUDE_CODE_SESSION_ID`` are excluded so one worker serves a project across
sessions, and ``CLAUDE_CONFIG_DIR`` is excluded so pooled accounts share a worker;
per-request routing gives each session its own state on that shared worker.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

PROTOCOL = 1

ENV_PREFIXES = ("CAPT_HOOK_", "CAPTAIN_HOOK_", "HOOKS_", "CLAUDE_", "FACTORY_")
ENV_EXACT = frozenset({"XDG_CACHE_HOME"})

WORKER_ENV_KEYS = frozenset(
    {
        "XDG_CACHE_HOME",
        "CAPTAIN_HOOK_STATE_DIR",
        "CAPTAIN_HOOK_LOG_DIR",
        "CAPTAIN_HOOK_TASKS_DIR",
        "CAPT_HOOK_DECISIONS_DB",
        "CAPT_HOOK_RUN_DIR",
    }
)

CLIENT_PATH = Path(__file__).with_name("client.py")


def request_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if k in ENV_EXACT or k.startswith(ENV_PREFIXES)}


def worker_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in WORKER_ENV_KEYS or k.startswith("HOOKS_")}


def worker_key(root: str, env: Mapping[str, str]) -> str:
    subset = worker_env(env)
    payload = "\0".join((os.path.realpath(root), *(f"{k}={subset[k]}" for k in sorted(subset))))
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def run_dir() -> Path:
    return Path(os.environ.get("CAPT_HOOK_RUN_DIR") or cache_home() / "captain-hook" / "run")


def socket_path(key: str) -> Path:
    return run_dir() / f"{key}.sock"


def lock_path(key: str) -> Path:
    return run_dir() / f"{key}.lock"


def meta_path(key: str) -> Path:
    return run_dir() / f"{key}.json"


def log_path(key: str) -> Path:
    return run_dir() / f"{key}.log"


def build_fingerprint() -> str:
    if override := os.environ.get("CAPT_HOOK_CLIENT_BUILD"):
        return override
    stat = CLIENT_PATH.stat()
    return f"{stat.st_mtime_ns}-{stat.st_size}"
