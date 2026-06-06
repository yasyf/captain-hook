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

Check file content with a string- or AST-mode check function. The mode is inferred from the
check's first parameter type; both return the violation strings spliced into `{violations}`:

```python
from captain_hook import lint

def find_prints(content: str) -> list[str]:
    return [line for line in content.splitlines() if "print(" in line]

lint(find_prints, message="Use logger.info() instead of print(): {violations}")
```

```python
import ast
from captain_hook import lint

def find_bare_except(tree: ast.AST) -> list[str]:
    return [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]

lint(find_bare_except, message="Avoid bare except clauses: {violations}")
```

Lints fire on `PostToolUse` for Edit/Write of `.py` files. Test files are skipped by default.

## styleguide

Apply AST-based style rules to Python edits, reporting only what the edit changed. A rule is a
[`StyleRule`][captain_hook.StyleRule] subclass whose docstring is the message and whose `match`
is a composable [`Matcher`][captain_hook.styleguide.Matcher]:

```python
from captain_hook import styleguide, StyleRule
from captain_hook.styleguide import Matcher

class NoPrint(StyleRule):
    """
    print() calls don't belong in committed code:
      - {violations}
    """
    match = Matcher.calls("print")
    label = "print() call"

styleguide(NoPrint)
```

captain-hook ships no rules of its own — `styleguide()` is the substrate (parsing,
change-scoping, formatting, test wiring). Each call registers one hook; scope it with
`only_if` / `skip_if` / `events` / `block`. See [Style Rules](styleguide.md) for the full guide,
including the `Matcher` algebra, `StyleDiffRule`, and change scoping.

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

## audit

Append a JSONL record per matching event for offline analysis:

```python
from captain_hook import audit, Event

audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)
```

By default, each line contains `ts`, `event`, `tool`, `file`, and a
session id derived from the transcript path. Records are written to
`$CLAUDE_PROJECT_DIR/.context/hook-logs/<YYYY-MM-DD>.jsonl`.

Customize the destination or record shape:

```python
from datetime import datetime
from captain_hook import audit, Event

audit(
    Event.PostToolUse,
    log_dir="logs/hooks",
    filename=lambda d: f"{d:%Y-%m-%dT%H}.jsonl",
    fields=lambda evt: {"event": evt.event_name.name, "tool": evt.tool_name},
)
```

| Parameter | Description |
|-----------|-------------|
| `events` | Event mask (default: `PreToolUse \| PostToolUse \| Stop`) |
| `log_dir` | Output directory (default: `$CLAUDE_PROJECT_DIR/.context/hook-logs`) |
| `filename` | `(datetime) -> str` mapping a timestamp to a filename |
| `fields` | `(evt) -> dict` for the per-record payload |
| `only_if` / `skip_if` | Condition lists |

---

!!! note "More primitives"
    For LLM-powered hooks (`llm_gate`, `llm_nudge`, `prompt_check`), see [LLM Hooks](llm-hooks.md).
    For multi-step enforcement, see [Workflows](workflows.md).
