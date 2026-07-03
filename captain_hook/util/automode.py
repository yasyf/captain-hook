from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import Field

from captain_hook.durable import DurableState
from captain_hook.prompt import dedent_text
from captain_hook.util.fs import resolve_binary

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent


class AutoModeDefaults(DurableState, scope="global"):
    version: str = ""
    rules: dict[str, Any] = Field(default_factory=dict)


def seeded_rules(evt: BaseHookEvent) -> dict[str, Any] | None:
    if (binary := resolve_binary("claude")) is None:
        return None
    if not (version := evt.ctx.call_cli([binary, "--version"], throw=False)):
        return None
    with AutoModeDefaults.mutate(evt) as defaults:
        if defaults.version != version:
            if not (out := evt.ctx.call_cli([binary, "auto-mode", "defaults"], throw=False)):
                return None
            try:
                parsed = json.loads(out)
            except ValueError:
                return None
            if not isinstance(parsed, dict):
                return None
            defaults.version = version
            defaults.rules = parsed
        return defaults.rules


def automode_rubric(evt: BaseHookEvent) -> str:
    if (rules := seeded_rules(evt)) is None:
        return dedent_text(
            """
            Baseline rules: treat as unsafe any command that deletes or overwrites data (rm,
            dd, shred, truncate, mkfs, git reset/clean/restore, force pushes), runs with
            elevated privileges (sudo), executes remotely fetched code (curl piped to a
            shell), writes outside the project, reads or exfiltrates credentials, or
            installs software system-wide. Reads, builds, tests, linters, and
            project-scoped edits are safe.
            """
        )
    return "Claude Code's auto-mode default permission rules, as JSON rule lists:\n" + json.dumps(rules, indent=2)
