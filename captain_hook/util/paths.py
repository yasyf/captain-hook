from __future__ import annotations

import os
from pathlib import Path


def resolve_state_dir() -> Path:
    return Path(os.environ.get("CAPTAIN_HOOK_STATE_DIR") or Path.home() / ".claude" / "state")


def resolve_project_dir() -> str | None:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR")


def resolve_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def resolve_cache_dir() -> Path:
    return resolve_cache_home() / "captain-hook"


def resolve_log_dir() -> Path:
    return Path(os.environ.get("CAPTAIN_HOOK_LOG_DIR") or resolve_cache_dir() / "logs")
