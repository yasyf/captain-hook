# Primitives Guide

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

Parameters:

:   `message` — warning text shown to the agent
:   `when` — predicate `(evt) -> bool` for additional filtering
:   `signals` — signal patterns for transcript text scoring
:   `only_if` / `skip_if` — condition lists
:   `events` — override default event targeting
:   `max_fires` — limit fires per session
:   `tests` — inline test dict
:   `async_` — run in the async dispatch pass

## gate

A blocking nudge — prevents the agent from proceeding:

```python
from captain_hook import gate, TouchedFile, RanCommand

gate("Run tests before stopping",
     skip_if=[RanCommand(r"uv\s+run\s+mtest")])
```

`gate()` is equivalent to `nudge(..., block=True)`.

**Default events:** `Stop | SubagentStop`.

## lint

Check file content with regex or AST patterns:

```python
from captain_hook import lint

# Regex lint — matches against file content
lint("no_print", r"print\(", message="Use logger.info() instead of print()")
```

```python
import ast
from captain_hook import lint

# AST lint — receives parsed AST, returns violation list
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

The token list is converted to a regex: `["git", "stash"]` → `r"git\s+stash"`.
Use `"*"` for wildcards: `["git", "stash", "*"]` matches `git stash pop`, `git stash drop`, etc.

## warn_command

Warn (but don't block) on specific commands:

```python
from captain_hook import warn_command

warn_command(
    ["python", "-c"],
    reason="Consider using mtest instead of raw python",
    when=lambda evt: "import bioqa" in evt.tool_input.get("command", ""),
)
```

## llm_gate

Gate with LLM-powered evaluation. The LLM receives transcript context
and a prompt, then returns a verdict:

```python
from captain_hook import llm_gate, Signal, Signals, Prompt

llm_gate(
    signals=Signals(
        patterns=[Signal(r"=\s*0.*because", weight=2)],
        threshold=2,
        window=15,
    ),
    prompt=Prompt()
        .system("You detect rationalized zero values.")
        .context("assistant_text", "{context}")
        .ask("Is the assistant rationalizing a zero/empty value? Reply block=true or block=false."),
    message="{r.reasoning}",
    max_fires=2,
)
```

The LLM returns a `GateVerdict(block: bool, reasoning: str)`. If `block=True`,
the hook blocks; otherwise it's skipped. `{r.reasoning}` in the message is
interpolated with the LLM's explanation.

## llm_nudge

Like `llm_gate` but warns instead of blocking:

```python
from captain_hook import llm_nudge, Signal, Signals, Prompt, RanCommand

llm_nudge(
    signals=Signals(
        patterns=[Signal(r"should contain", weight=2)],
        threshold=3,
        window=10,
    ),
    prompt=Prompt()
        .system("You detect speculation without evidence.")
        .context("assistant_text", "{context}")
        .ask("Is the assistant speculating? Reply fire=true or fire=false."),
    message="{r.reasoning}",
    skip_if=[RanCommand(r"uv\s+run\s+lf")],
)
```

## prompt_check

LLM-evaluate content (e.g., test quality) with a template prompt:

```python
from captain_hook import prompt_check

def check_quality(evt):
    return prompt_check(
        evt,
        template="Evaluate this test:\n{code}\nIs it structural-only?",
        vars={"code": evt.tool_input.get("content", "")},
        prefix="Test quality issue",
        suffix="Write behavioral assertions",
    )
```

## workflow

Multi-step workflows that gate the agent until all steps are complete:

```python
from captain_hook import workflow, Step, Artifact, text_matches

workflow(
    label="REVIEW",
    marker="REVIEW COMPLETE",
    steps=[
        Step(
            name="run linter",
            check=text_matches(r"ruff check"),
            stopped_at="Run the linter first",
            next_step="Run: ruff check src/",
        ),
        Step(
            name="run tests",
            check=text_matches(r"pytest.*passed"),
            stopped_at="Run the test suite",
            next_step="Run: pytest tests/ -x",
        ),
    ],
    artifacts=[
        Artifact(path="reports/lint.json"),
    ],
)
```

The workflow:

1. Checks if the `marker` text appears in the transcript
2. If not, evaluates each `step` — the first incomplete step gates the agent
3. If all steps pass, validates `artifacts` exist on disk
4. If everything passes, calls `post_complete(evt)` if provided

Workflows register on `SubagentStop` with `max_fires=1`.
