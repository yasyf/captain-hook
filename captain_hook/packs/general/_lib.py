"""Conditions shared by the general pack's hook modules; the ``_`` prefix keeps the loader from registering it."""

from __future__ import annotations

from captain_hook import BaseHookEvent, CustomCondition

# Prose and config file extensions that shouldn't, on their own, demand a code-review pass.
# Tailor this (and the excluded dirs below) to scope what counts as "source" for your repo.
NON_SOURCE_SUFFIXES = (
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".lock",
)


class EditedSource(CustomCondition):
    """True when the session edited a non-test source file (docs and config excluded)."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            not f.is_test and f.suffix not in NON_SOURCE_SUFFIXES and not f.under("docs", ".claude", ".github")
            for f in evt.ctx.t.tool_calls.named("Edit|Write").files()
        )
