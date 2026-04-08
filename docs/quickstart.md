# Quickstart

Write your first hook in under 5 minutes.

## Your first hook: block a dangerous command

Create a file `my_hooks.py` in your hooks directory:

```python
from captain_hook import Event, hook, Command

hook(
    Event.PreToolUse,
    message="rm -rf is not allowed in this project",
    block=True,
    only_if=[Command(r"rm\s+-rf")],
)
```

That's it — 7 lines, and Claude Code will be blocked from running any `rm -rf` command.

## How it works

1. **`Event.PreToolUse`** — the hook fires before a tool is executed
2. **`message=`** — the text shown to Claude when the hook triggers
3. **`block=True`** — prevents the action (use `block=False` for a warning)
4. **`only_if=[Command(...)]`** — only match Bash commands matching the regex

## Three ways to register hooks

### 1. Declarative (most hooks)

```python
from captain_hook import hook, Event, Tool

hook(Event.PreToolUse, message="Don't use grep", block=True, only_if=[Tool("Grep")])
```

### 2. Primitives (common patterns)

```python
from captain_hook import block_command, nudge, lint, TouchedFile

block_command(["git", "stash"], reason="Use jj instead")
nudge("Remember to run tests", only_if=[TouchedFile("**/*.py")])
lint("no_print", r"print\(", message="Use logger instead")
```

### 3. Handler functions (complex logic)

```python
from captain_hook import HookApp, Event, Tool, get_current_app

app = get_current_app()

@app.on(Event.PreToolUse, only_if=[Tool("Bash")])
def check_command(evt):
    if "sudo" in evt.tool_input.get("command", ""):
        return evt.block("sudo is not allowed")
```

## Running hooks

The CLI reads a JSON event from stdin and writes the result to stdout:

```bash
echo '{"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}' \
  | python -m captain_hook run PreToolUse --hooks src/
```

Output:

```json
{"permissionDecision": "deny", "permissionDecisionReason": "rm -rf is not allowed in this project"}
```

## Next steps

- [Core Concepts](core-concepts.md) — understand events, conditions, and dispatch
- [Primitives Guide](primitives.md) — explore all built-in hook primitives
- [Testing Guide](testing.md) — test your hooks with inline tests
