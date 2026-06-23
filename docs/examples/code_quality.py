from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from captain_hook import (
    Allow,
    Event,
    Input,
    Pattern,
    Signal,
    Signals,
    SourceEdits,
    TestFile,
    Warn,
    hook,
    lint,
    llm_gate,
    nudge,
)


def bare_excepts(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        yield f"line {node.lineno}"


hook(
    Event.PostToolUse,
    only_if=[SourceEdits(lang="py"), Pattern("print($$$)")],
    skip_if=[TestFile()],
    message="Use the project logger instead of print(). See docs/logging.md.",
    tests={
        Input(tool="Edit", file="src/app.py", content='import sys\nprint("debug")\n'): Warn(pattern="logger"),
        Input(tool="Edit", file="src/app.py", content="logger.info('ok')\n"): Allow(),
        Input(tool="Edit", file="src/app.py", content="label = 'print(x)'  # only in a string\n"): Allow(),
    },
)


lint(
    bare_excepts,
    message="Bare except clauses silently swallow errors. Catch a specific exception type instead: {violations}",
    tests={
        Input(tool="Edit", file="src/app.py", content="try:\n    f()\nexcept:\n    pass\n"): Warn(pattern="swallow"),
        Input(tool="Edit", file="src/app.py", content="try:\n    f()\nexcept ValueError:\n    pass\n"): Allow(),
    },
)


nudge(
    "You keep adding print()s after edits. Switch to logger.debug() and tail the log.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"print\(", weight=1, flags=re.MULTILINE),
            Signal(pattern=r"debug[\s_-]print", weight=2, flags=re.IGNORECASE),
        ],
        threshold=3,
        window=8,
    ),
)


llm_gate(
    "Does this diff add a print() that should be a logger call, where the surrounding "
    "module already imports a logger? Block only if the prod print is unambiguous.",
    message=lambda r: f"Replace print() with logger: {r.reasoning}",
    events=Event.PostToolUse,
    only_if=[SourceEdits(lang="py"), Pattern("print($$$)")],
    skip_if=[TestFile()],
    max_fires=2,
)
