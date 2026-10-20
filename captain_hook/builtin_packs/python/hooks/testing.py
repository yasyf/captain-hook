from __future__ import annotations

from captain_hook import (
    Allow,
    Input,
    TestFile,
    Tool,
    Warn,
    nudge,
)

nudge(
    "Verify test edits with the narrowest run, not the whole suite. Run `uv run pytest path/to/test.py::test_name`.",
    only_if=[Tool("Edit|Write"), TestFile()],
    tests={
        Input(file="tests/test_mod.py", content="def test_x(): ..."): Warn(),
        Input(file="pkg/mod.py", content="x = 1"): Allow(),
    },
)
