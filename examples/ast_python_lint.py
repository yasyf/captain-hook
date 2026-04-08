# Example: AST-based Python lint
#
# Demonstrates how to use `lint` with an AST-mode checker to warn
# about bare `assert` statements in production code.  The checker
# receives each AST node and yields a violation string for every
# `assert` it finds.

import ast
from collections.abc import Iterator

from captain_hook import lint


def find_bare_asserts(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Assert):
        yield f"line {node.lineno}: bare assert"


# Register the lint — fires on PostToolUse for Write/Edit when
# the written content contains "assert".
lint(
    find_bare_asserts,
    message="Avoid bare assert in production code: {violations}",
    trigger="assert",
)
