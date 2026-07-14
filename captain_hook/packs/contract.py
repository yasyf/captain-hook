"""Shared pack-contract identity: the captain-hook marketplace/plugin slugs, the canonical
attach command shape, and the argv predicates that both ``pack lint`` and ``pack attach``
bootstrapping tokenize against. Imported by ``cli`` and ``packs.bootstrap`` alike; it imports
nothing from either, so the shared constants live here without a cycle."""

from __future__ import annotations

import re
import shlex
from typing import Any

DIST_NAME = "capt-hook"
DEFAULT_PREFIX = f"uvx --isolated {DIST_NAME}"
PLUGIN_ID = "captain-hook@captain-hook"
MARKETPLACE_NAME = "captain-hook"
MARKETPLACE_REPO = "yasyf/captain-hook"

PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
# The two accepted attach dir args, double-quoted exactly: shlex strips the quotes, so the
# quoting is verified against the raw command string, not the tokenized argv.
ATTACH_DIR_ROOT = f'"{PLUGIN_ROOT_VAR}"'
ATTACH_DIR_HOOKS = f'"{PLUGIN_ROOT_VAR}/hooks"'
# A version floor is a lower-bound constraint: `>=X.Y.Z` (a bare pin or `<=`/`==` doesn't let a
# newer captain-hook resolve).
VERSION_FLOOR_RE = re.compile(r">=\s*\d+\.\d+\.\d+")


def attach_command(*, nested: bool) -> str:
    """The canonical SessionStart attach line for a pack whose manifest resolves ``nested`` or not."""
    return f"{DEFAULT_PREFIX} pack attach {ATTACH_DIR_HOOKS if nested else ATTACH_DIR_ROOT}"


def command_entries(hooks_json: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(event, command)`` command-type hook entry across the file, in declaration order."""
    return [
        (event, entry["command"])
        for event, groups in hooks_json.get("hooks", {}).items()
        for group in groups
        for entry in group.get("hooks", [])
        if entry.get("type") == "command"
    ]


def is_attach_argv(argv: list[str]) -> bool:
    """True when ``argv`` is exactly the canonical prefix + ``pack attach <one directory arg>``."""
    prefix = shlex.split(DEFAULT_PREFIX)
    return (
        len(argv) == len(prefix) + 3
        and argv[: len(prefix)] == prefix
        and argv[len(prefix) : len(prefix) + 2] == ["pack", "attach"]
    )


def is_canonical_run_argv(argv: list[str]) -> bool:
    """True when ``argv`` is the canonical ``<prefix> run <...>`` a legacy consumer mirrors."""
    prefix = shlex.split(DEFAULT_PREFIX)
    return len(argv) > len(prefix) and argv[: len(prefix)] == prefix and argv[len(prefix)] == "run"


def attach_dir_reason(cmd: str, *, nested: bool) -> str | None:
    """None when ``cmd``'s attach dir is the correctly-quoted plugin-root form for the layout; else why not.

    The dir arg must be the double-quoted ``"${CLAUDE_PLUGIN_ROOT}"`` (manifest at the plugin
    root) or ``"${CLAUDE_PLUGIN_ROOT}/hooks"`` (manifest one ``hooks/`` level down). shlex strips
    the quotes, so the raw command is checked: an unquoted var, a literal path, or the wrong
    suffix for the resolved layout all fail.
    """
    expected, other = (ATTACH_DIR_HOOKS, ATTACH_DIR_ROOT) if nested else (ATTACH_DIR_ROOT, ATTACH_DIR_HOOKS)
    where = "one hooks/ level below the plugin root" if nested else "at the plugin root"
    if expected in cmd:
        return None
    if other in cmd:
        return f"attach targets {other} but the manifest resolves {where}; use {expected}"
    return f"attach dir must be the double-quoted {expected} (the manifest resolves {where})"
