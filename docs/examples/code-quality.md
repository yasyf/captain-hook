# Code Quality

*Catch anti-patterns in real time as the agent writes code.*

## The problem

Claude writes `print()` statements instead of using your project's logger. It also occasionally uses bare `except:` clauses that silently swallow errors. These patterns pass silently -- you only notice them during code review, long after the agent has moved on.

## The solution

### Regex lint for print statements

The simplest lint is a string check. It receives the file content as a string and returns a list of violation descriptions.

```python
from captain_hook import lint, Input, Warn, Allow

def find_prints(content: str) -> list[str]:
    return [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(content.splitlines(), 1)
        if "print(" in line
    ]

lint(
    find_prints,
    message="Use logger instead of print(): {violations}",
    tests={
        Input(tool="Edit", file="app/main.py", content='print("debug")\n'): Warn(),
        Input(tool="Edit", file="app/main.py", content='logger.info("debug")\n'): Allow(),
        Input(tool="Edit", file="tests/test_app.py", content='print("debug")\n'): Allow(),
    },
)
```

!!! note
    Lints automatically skip test files (`test_*.py`, `conftest.py`) and only fire on `Edit` and `Write` tools targeting `.py` files. You do not need to configure this -- it is the default behavior.

### AST lint for bare except clauses

For structural checks, use an AST-mode checker. It receives each AST node and yields violation strings.

```python
import ast
from collections.abc import Iterator
from captain_hook import lint, Input, Warn, Allow

def find_bare_excepts(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        yield f"line {node.lineno}: bare except clause"

lint(
    find_bare_excepts,
    message="Use explicit exception types: {violations}",
    trigger="except",
    tests={
        Input(
            tool="Write",
            file="app/handler.py",
            content="try:\n    run()\nexcept:\n    pass\n",
        ): Warn(),
        Input(
            tool="Write",
            file="app/handler.py",
            content="try:\n    run()\nexcept ValueError:\n    pass\n",
        ): Allow(),
    },
)
```

The `trigger` parameter is an optimization: the AST is only parsed when the source contains the trigger string. For `except`, this avoids parsing files that have no exception handlers at all.

### Blocking lint for critical violations

By default, lints warn. For violations that must never ship, set `block=True`:

```python
import ast
from collections.abc import Iterator
from captain_hook import lint

def find_hardcoded_secrets(node: ast.AST) -> Iterator[str]:
    if (
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and "secret" in t.id.lower()
            for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ):
        yield f"line {node.lineno}: hardcoded secret"

lint(
    find_hardcoded_secrets,
    message="Never hardcode secrets: {violations}",
    trigger="secret",
    block=True,
)
```

### How violations appear

When a lint fires, the agent sees the violation list interpolated into the message:

```
Use logger instead of print(): line 12: print("connecting..."), line 45: print(result)
```

The `{violations}` placeholder is replaced with the first 5 violations joined by commas. If there are more, they are truncated to keep the message readable.

!!! tip
    String-mode and AST-mode are detected automatically from the type hint of the check function's first parameter. If it accepts `str`, captain-hook runs string mode. If it accepts `ast.AST`, it walks the tree and calls the check on every node.
