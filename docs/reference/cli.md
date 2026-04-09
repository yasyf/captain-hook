# CLI Reference

captain-hook provides a CLI for scaffolding, dispatching, testing, and generating settings.

```
captain-hook [-h] [--hooks HOOKS] [--root ROOT] {init,run,test,generate-settings} ...
```

## Global options

| Option | Default | Description |
|--------|---------|-------------|
| `--hooks HOOKS` | `src` | Path to the hooks package directory |
| `--root ROOT` | Current directory | Project root for gitignore and session resolution |

---

## `init`

Scaffold a hooks directory with an example hook, entrypoint script, and Claude Code settings.

```bash
captain-hook init
```

Creates:

```
.claude/hooks/
├── src/
│   └── hooks.py          # Example hook file
├── bin/
│   └── hooks             # Entrypoint script (chmod +x)
└── pyproject.toml        # Package config
```

The entrypoint script is what Claude Code calls. It invokes `captain-hook run` with the right paths.

!!! tip
    After running `init`, run `captain-hook generate-settings` to wire up the hooks in Claude Code's settings.

---

## `run`

Dispatch a hook event. Reads a JSON event payload from stdin, runs it through all registered hooks, and writes the result to stdout.

```bash
echo '{"tool_name": "Bash", "tool_input": {"command": "git stash"}}' \
  | captain-hook run PreToolUse --hooks src/
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `EVENT` | Event type: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SubagentStop`, `SubagentStart`, `UserPromptSubmit`, `Notification`, `PreCompact` |

**Output** (stdout):

For a block:
```json
{"permissionDecision": "deny", "permissionDecisionReason": "git stash is not allowed; use jj"}
```

For a warning:
```json
{"permissionDecision": "allow", "additionalContext": "Remember to run tests"}
```

For no match (allow):
```json
{}
```

!!! note
    Claude Code calls this command automatically via the entrypoint script. You typically don't call `run` directly except for debugging.

---

## `test`

Run all inline tests defined in registered hooks.

```bash
captain-hook test --hooks src/
```

Discovers all hooks, collects their `tests` dicts, and runs each `Input` through the dispatch pipeline, comparing results against `Block`, `Warn`, or `Allow` expectations.

**Output:**

```
✓ block_command: git stash → Block (matched "jj")
✓ block_command: git status → Allow
✓ lint: print() in code → Warn (matched "logger")
✗ gate: stop without tests → expected Block, got Allow

3 passed, 1 failed
```

!!! tip
    Inline tests verify conditions and dispatch logic deterministically. LLM-powered hooks have their `call_llm` stubbed with a positive verdict — inline tests validate *signal matching*, not LLM judgment.

---

## `generate-settings`

Generate Claude Code settings JSON for `.claude/settings.local.json`.

```bash
captain-hook generate-settings --hooks src/ --root .
```

**Output** (stdout):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "command",
        "command": ".claude/hooks/bin/hooks PreToolUse"
      }
    ],
    "PostToolUse": [
      {
        "type": "command",
        "command": ".claude/hooks/bin/hooks PostToolUse"
      }
    ]
  }
}
```

Only includes event types that have at least one registered hook. Pipe to a file or merge into your existing settings:

```bash
captain-hook generate-settings --hooks src/ > .claude/settings.local.json
```

---

## Integration with Claude Code

The typical setup flow:

```bash
captain-hook init                           # scaffold project
# edit .claude/hooks/src/hooks.py           # write your hooks
captain-hook test --hooks .claude/hooks/src  # verify
captain-hook generate-settings \
  --hooks .claude/hooks/src \
  --root . > .claude/settings.local.json    # wire up
```

Claude Code reads `.claude/settings.local.json` on startup and calls the entrypoint script for each registered event type.
