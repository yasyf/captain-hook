# Example: AST-based Python lint
#
# Demonstrates how to use `lint` with an AST-mode checker to warn
# about bare `assert` statements in production code.  The checker
# receives each AST node and yields a violation string for every
# `assert` it finds.

import ast
from collections.abc import Iterator

from captain_hook import (
    HookApp,
    execute_hook,
    lint,
    mock_tool_event,
)
from captain_hook.app import _current_app
from captain_hook.types import Event

app = HookApp()
token = _current_app.set(app)


def find_bare_asserts(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Assert):
        yield f"line {node.lineno}: bare assert"


lint(
    find_bare_asserts,
    message="Avoid bare assert in production code: {violations}",
    trigger="assert",
)

_current_app.reset(token)

source_with_assert = """\
def divide(a, b):
    assert b != 0, "divisor must be nonzero"
    return a / b
"""

evt = mock_tool_event(
    "Write",
    event=Event.PostToolUse,
    file="mathlib.py",
    content=source_with_assert,
)
result = execute_hook(app.hooks[0], evt)
assert result is not None
assert result.action == "warn"
assert "bare assert" in result.message

clean_source = """\
def divide(a, b):
    if b == 0:
        raise ValueError("divisor must be nonzero")
    return a / b
"""

evt_clean = mock_tool_event(
    "Write",
    event=Event.PostToolUse,
    file="mathlib.py",
    content=clean_source,
)
result_clean = execute_hook(app.hooks[0], evt_clean)
assert result_clean is None

print("✓ AST lint caught bare assert, clean code passed")
