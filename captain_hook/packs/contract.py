"""Shared pack-contract identity: the captain-hook marketplace/plugin slugs, the canonical
attach command shape, and the argv predicates that both ``pack lint`` and ``pack attach``
bootstrapping tokenize against. Imported by ``cli`` and ``packs.bootstrap`` alike; it imports
nothing from either, so the shared constants live here without a cycle."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
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
# Shell control operators/redirections/substitutions: a canonical grammar token carries none.
SHELL_METACHARS = frozenset(";&|<>()`\n\r")


def attach_command(*, nested: bool) -> str:
    """The canonical SessionStart attach line for a pack whose manifest resolves ``nested`` or not."""
    return f"{DEFAULT_PREFIX} pack attach {ATTACH_DIR_HOOKS if nested else ATTACH_DIR_ROOT}"


def search_upward(start: Path, *rel: str, stop: Path | None = None) -> Path | None:
    """The nearest existing ``base/rel`` walking from ``start`` upward; when ``stop`` is given the
    walk halts at ``stop`` (inclusive) and never ascends above it."""
    bound = stop.resolve() if stop is not None else None
    for base in (start, *start.parents):
        for r in rel:
            if (cand := base / r).is_file():
                return cand
        if bound is not None and base.resolve() == bound:
            break
    return None


def command_entries(hooks_json: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(event, command)`` command-type hook entry across the file, in declaration order.

    Malformed shapes (a non-dict ``hooks`` map, a non-list group list, a non-dict group or entry,
    a command entry without a string ``command``) are skipped rather than raised, so ``pack lint``
    surveys a hand-edited file without tracebacking; ``scaffold`` refuses those shapes up front.
    """
    hooks = hooks_json.get("hooks", {})
    return [
        (event, entry["command"])
        for event, groups in (hooks.items() if isinstance(hooks, dict) else ())
        for group in (groups if isinstance(groups, list) else ())
        for entry in (group.get("hooks", []) if isinstance(group, dict) else ())
        if isinstance(entry, dict) and entry.get("type") == "command" and isinstance(entry.get("command"), str)
    ]


def carries_shell_operator(argv: list[str]) -> bool:
    """True when any token carries a shell control operator, redirection, or substitution char."""
    return any(SHELL_METACHARS & set(token) for token in argv)


def is_attach_argv(argv: list[str]) -> bool:
    """True when ``argv`` is exactly the canonical prefix + ``pack attach <one plain directory arg>``."""
    prefix = shlex.split(DEFAULT_PREFIX)
    return (
        len(argv) == len(prefix) + 3
        and argv[: len(prefix)] == prefix
        and argv[len(prefix) : len(prefix) + 2] == ["pack", "attach"]
        and not carries_shell_operator(argv)
    )


def is_canonical_run_argv(argv: list[str]) -> bool:
    """True when ``argv`` is exactly ``<prefix> run <Event>`` optionally trailed by ``--async``."""
    prefix = shlex.split(DEFAULT_PREFIX)
    rest = argv[len(prefix) :]
    return (
        argv[: len(prefix)] == prefix
        and rest[:1] == ["run"]
        and len(rest) in (2, 3)
        and (len(rest) == 2 or rest[2] == "--async")
        and not carries_shell_operator(argv)
    )


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
