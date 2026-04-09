# Primitives

Primitives are convenience wrappers that register hooks for common patterns.
They handle event targeting, fire counting, and echo suppression automatically.

## nudge

Warn the agent when conditions or signals match:

```python
from captain_hook import nudge, TouchedFile

nudge("Remember to run tests after editing Python files",
      only_if=[TouchedFile("**/*.py")])
```

With signal scoring (fires when transcript text matches patterns):

```python
from captain_hook import nudge, Signal, Signals

nudge(
    "Stop retrying — narrow the failing test first",
    signals=Signals(
        patterns=[
            Signal(r"let me try again", weight=2),
            Signal(r"retry", weight=1),
        ],
        threshold=3,
        window=10,
    ),
)
```

**Default events:** `PostToolUse` (with signals) or `PreToolUse` (without).
**Default max_fires:** 3 (with signals) or 1 (without).

| Parameter | Description |
|-----------|-------------|
| `message` | Warning text shown to the agent |
| `when` | Predicate `(evt) -> bool` for additional filtering |
| `signals` | Signal patterns for transcript text scoring |
| `only_if` / `skip_if` | Condition lists |
| `events` | Override default event targeting |
| `max_fires` | Limit fires per session |
| `tests` | Inline test dict |

## gate

A blocking nudge — prevents the agent from proceeding:

```python
from captain_hook import gate, RanCommand

gate("Run tests before stopping",
     skip_if=[RanCommand(r"uv\s+run\s+mtest")])
```

`gate()` is equivalent to `nudge(..., block=True)`.

**Default events:** `Stop | SubagentStop`.

## lint

Check file content with regex or AST patterns:

```python
from captain_hook import lint

lint("no_print", r"print\(", message="Use logger.info() instead of print()")
```

```python
import ast
from captain_hook import lint

def find_bare_except(tree: ast.AST) -> list[str]:
    return [
        f"Line {node.lineno}: bare except"
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]

lint("no_bare_except", find_bare_except, message="Avoid bare except clauses")
```

Lints fire on `PostToolUse` for Edit/Write of `.py` files. Test files are skipped by default.

## block_command

Block specific bash commands:

```python
from captain_hook import block_command

block_command(
    ["git", "stash"],
    reason="git stash is not allowed; use jj",
    hint="Run `jj shelve` instead",
)
```

The token list is converted to a regex: `["git", "stash"]` becomes `r"git\s+stash"`.
Use `"*"` for wildcards: `["git", "stash", "*"]` matches `git stash pop`, `git stash drop`, etc.

## warn_command

Warn (but don't block) on specific commands:

```python
from captain_hook import warn_command

warn_command(
    ["python", "-c"],
    message="Consider using mtest instead of raw python",
)
```

---

!!! note "More primitives"
    For LLM-powered hooks (`llm_gate`, `llm_nudge`, `prompt_check`), see [LLM Hooks](llm-hooks.md).
    For multi-step enforcement, see [Workflows](workflows.md).
