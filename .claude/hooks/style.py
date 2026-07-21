from __future__ import annotations

from captain_hook import TouchedFile, gate

# Repo-specific overlay on top of the `python` pack (the StyleRule set comes from the
# pack; only this captain-hook-scoped session-end review gate lives here).

gate(
    "You wrote new Python code but haven't done a style review. Before stopping, "
    "review your changes against STYLEGUIDE.md (functional over imperative, no underscore "
    "prefixes, match for type dispatch, minimal try/except, make invalid states "
    "unrepresentable, flat over nested). Fix any violations in the code you wrote.",
    only_if=[TouchedFile("**/captain_hook/**/*.py", subagents=True)],
)
