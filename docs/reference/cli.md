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

Scaffold a hooks directory with an example hook and Claude Code settings.

```bash
captain-hook init
```

Creates:

```
.claude/
├── hooks/
│   └── example.py        # Example hook file
└── settings.local.json   # Claude Code hook settings
```

The generated settings wire Claude Code to call `uv run captain-hook` for each registered event.

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

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--hooks-dir` | `.claude/hooks` | Hooks directory relative to project root |
| `--no-merge` | | Output standalone JSON instead of merging |

**Output** (stdout):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "command",
        "command": "uv run --directory $CLAUDE_PROJECT_DIR captain-hook --hooks $CLAUDE_PROJECT_DIR/.claude/hooks --root $CLAUDE_PROJECT_DIR run PreToolUse"
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
uv add captain-hook                         # add as dependency
captain-hook init                           # scaffold project
# edit .claude/hooks/my_hooks.py            # write your hooks
captain-hook test --hooks .claude/hooks      # verify
captain-hook generate-settings \
  --hooks .claude/hooks \
  --root .                                  # regenerate settings
```

Claude Code reads `.claude/settings.local.json` on startup and calls `uv run captain-hook` for each registered event type.
