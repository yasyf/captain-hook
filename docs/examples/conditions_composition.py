"""Flag raw SQL written into application Python by composing conditions."""

from __future__ import annotations

from captain_hook import (
    Allow,
    Content,
    Event,
    FilePath,
    Input,
    SourceEdits,
    Warn,
    hook,
)

# Compose conditions to target exactly the right edit. only_if is AND, skip_if is
# any-skips, and SourceEdits already narrows to non-test source in one language.
# Here: a raw SQL string written into application Python, but not into migrations
# (skipped by path) and not into tests (SourceEdits excludes test files by default).
hook(
    Event.PostToolUse,
    message="Raw SQL in application code. Route queries through db/queries.py.",
    only_if=[SourceEdits(lang="py", paths="app/**"), Content(r"(?i)\bselect\b.+\bfrom\b")],
    skip_if=[FilePath("**/migrations/**")],
    tests={
        Input(tool="Edit", file="app/users.py", content='q = "SELECT id FROM users"\n'): Warn(),
        Input(tool="Edit", file="app/migrations/0001.py", content='"SELECT 1 FROM t"\n'): Allow(),
        Input(tool="Edit", file="app/tests/test_users.py", content='"SELECT id FROM users"\n'): Allow(),
        Input(tool="Edit", file="app/util.py", content="x = 1  # nothing to flag here\n"): Allow(),
    },
)
