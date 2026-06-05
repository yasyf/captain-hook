from __future__ import annotations

from captain_hook import gate

gate(
    "You wrote new Python code but haven't done a style review. Before stopping, "
    "review your changes against AGENTS.md § Python Style (functional over imperative, "
    "no underscore prefixes, match statements for type dispatch, minimal try/except, "
    "flat over nested). Fix any violations in the code you wrote.",
    when=lambda evt: any(
        f.matches("**/captain_hook/**/*.py") and not f.is_test for f in evt.ctx.t.extract_files(["Edit", "Write"])
    ),
)
