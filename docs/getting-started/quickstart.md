# Quickstart

Write your first hook in under 5 minutes.

## Your first hook: block a dangerous command

Create `.claude/hooks/my_first.py`:

```python
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["rm", "-rf", "*"],
    reason="Recursive force-delete is forbidden",
    hint="Delete files individually or use a safer alternative",
    tests={
        Input(command="rm -rf build/"): Block(),
        Input(command="rm file.txt"): Allow(),
    },
)
```

That's it — Claude Code will be blocked from running any `rm -rf` command.

## How it works

1. **`block_command`** registers a `PreToolUse` hook on Bash commands
2. The token list `["rm", "-rf", "*"]` becomes the regex `rm\s+-rf\s+\S+`
3. When Claude tries to run a matching command, captain-hook returns a **block** with your reason and hint

## Three ways to register hooks

### 1. Primitives (most hooks)

```python
from captain_hook import block_command, nudge, gate, lint, TouchedFile, RanCommand

block_command(["git", "stash"], reason="Use jj instead")
nudge("Remember to run tests", only_if=[TouchedFile("**/*.py")])
gate("Run tests before stopping", skip_if=[RanCommand(r"pytest")])
lint("no_print", r"print\(", message="Use logger instead")
```

### 2. Declarative

```python
from captain_hook import hook, Event, Tool, Command

hook(
    Event.PreToolUse,
    message="Don't use grep directly",
    block=True,
    only_if=[Tool("Grep")],
)
```

### 3. Handler functions (complex logic)

```python
from captain_hook import on, Event, Tool

@on(Event.PreToolUse, only_if=[Tool("Bash")])
def check_command(evt):
    if "sudo" in evt.tool_input.get("command", ""):
        return evt.block("sudo is not allowed")
```

## Test your hooks

Add inline tests right next to the hook definition:

```python
from captain_hook import block_command, Input, Block, Allow

block_command(
    ["git", "stash"],
    reason="Use jj instead",
    tests={
        Input(command="git stash"): Block("jj"),
        Input(command="git status"): Allow(),
    },
)
```

Run them (from your project root, `--hooks` defaults to `.claude/hooks`):

```bash
captain-hook test
```

Sample output for the hook above:

```
.claude/hooks/my_first.py
  PASS  Input(command='rm -rf build/')   expected Block, got Block
  PASS  Input(command='rm file.txt')     expected Allow, got Allow

2 passed, 0 failed
```

If a test fails, the line shows the expected vs. actual `Action` and exits non-zero — drop the same command into CI and a regressed hook fails the build.

## Run from the CLI

```bash
echo '{"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}' \
  | captain-hook run PreToolUse
```

Output:

```json
{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: Recursive force-delete is forbidden. Delete files individually or use a safer alternative."}
```

## Next steps

- [Patterns](../guide/patterns.md) — real-world hooks from a production hooks directory
- [How It Works](how-it-works.md) — understand the lifecycle and dispatch pipeline
- [Primitives](../guide/primitives.md) — explore all built-in hook primitives
- [Conditions](../guide/conditions.md) — filter when hooks fire
- [Testing](../guide/testing.md) — test your hooks with inline tests and mock events
