from __future__ import annotations

import os
from pathlib import Path

from captain_hook.util import reqenv


def resolve_target(token: str, cwd: Path | None) -> Path | None:
    path = Path(os.path.expanduser(token))
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    return path.parent.resolve() / path.name


def resolve_state_dir() -> Path:
    return Path(reqenv.getenv("CAPTAIN_HOOK_STATE_DIR") or Path.home() / ".claude" / "state")


def resolve_claude_config_dir() -> Path:
    return Path(reqenv.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def resolve_project_dir() -> str | None:
    return reqenv.getenv("CLAUDE_PROJECT_DIR") or reqenv.getenv("FACTORY_PROJECT_DIR")


def resolve_cache_home() -> Path:
    return Path(reqenv.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")


def resolve_cache_dir() -> Path:
    return resolve_cache_home() / "captain-hook"


def resolve_log_dir() -> Path:
    return Path(reqenv.getenv("CAPTAIN_HOOK_LOG_DIR") or resolve_cache_dir() / "logs")
