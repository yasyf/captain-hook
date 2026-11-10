from __future__ import annotations

import os
import shutil
from functools import cache
from pathlib import Path

from captain_hook.util import reqenv

MOVED_STATE = ("hooks", "review", "faults", "update")


def resolve_target(token: str, cwd: Path | None) -> Path | None:
    path = Path(os.path.expanduser(token))
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    return path.parent.resolve() / path.name


def resolve_state_dir() -> Path:
    if override := reqenv.getenv("CAPTAIN_HOOK_STATE_DIR"):
        return Path(override)
    return adopted_state_dir(Path(reqenv.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "captain-hook")


@cache
def adopted_state_dir(target: Path) -> Path:
    """*target*, once it holds the state earlier releases kept under ``~/.claude/state``.

    Every write under ``~/.claude`` wakes the file watcher of each running Claude session, so the
    state moved out, and a name already at *target*, a symlink to it included, stays where it is.
    """
    legacy = Path.home() / ".claude" / "state"
    target.mkdir(parents=True, exist_ok=True)
    for name in MOVED_STATE:
        if (legacy / name).exists() and not (target / name).exists():
            try:
                shutil.move(legacy / name, target / name)
            except OSError:
                from loguru import logger

                logger.opt(exception=True).warning(f"could not move {legacy / name} to {target}")
    return target


def resolve_claude_config_dir() -> Path:
    return Path(reqenv.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def resolve_long_running_state() -> Path:
    if explicit := reqenv.getenv("LONG_RUNNING_STATE"):
        return Path(explicit)
    return Path(reqenv.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "long-running"


def resolve_project_dir() -> str | None:
    return reqenv.getenv("CLAUDE_PROJECT_DIR") or reqenv.getenv("FACTORY_PROJECT_DIR")


def resolve_cache_home() -> Path:
    return Path(reqenv.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")


def resolve_cache_dir() -> Path:
    return resolve_cache_home() / "captain-hook"


def resolve_log_dir() -> Path:
    return Path(reqenv.getenv("CAPTAIN_HOOK_LOG_DIR") or resolve_cache_dir() / "logs")
