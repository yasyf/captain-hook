# CLI Reference

captain-hook provides a CLI for scaffolding, dispatching, testing, and generating settings.

```
capt-hook [-h] [--hooks HOOKS] [--root ROOT] {run,generate-settings,test,init,logs} ...
```

## Global options

| Option | Default | Description |
|--------|---------|-------------|
| `--hooks HOOKS` | `$CLAUDE_PROJECT_DIR/.claude/hooks` (cwd-relative when unset) | Path to the hooks package directory |
| `--root ROOT` | `$CLAUDE_PROJECT_DIR` (current directory when unset) | Project root for gitignore and session resolution |

---

## `init`

Scaffold a hooks directory with an example hook and Claude Code settings.

```bash
capt-hook init
```

Creates:

```
.claude/
├── hooks/
│   └── example.py        # Example hook file
└── settings.local.json   # Claude Code hook settings
```

The generated settings wire Claude Code to call `uvx capt-hook` for each registered event.

!!! tip
    After running `init`, run `capt-hook generate-settings` to wire up the hooks in Claude Code's settings.

---

## `run`

Dispatch a hook event. Reads a JSON event payload from stdin, runs it through all registered hooks, and writes the result to stdout.

```bash
echo '{"tool_name": "Bash", "tool_input": {"command": "git stash"}}' \
  | capt-hook --hooks src/ run PreToolUse
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
capt-hook --hooks src/ test
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
capt-hook --hooks src/ --root . generate-settings
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--hooks-dir` | `.claude/hooks` | Hooks directory relative to project root |
| `--no-merge` | | Output standalone JSON instead of merging |
| `--from` | `capt-hook` | Package source for `uvx` (local path or PyPI spec). Defaults to the published distribution, in which case the generated command omits `--from`; pass a local path to point `uvx` at a checkout. |

**Output** (stdout):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "command",
        "command": "uvx capt-hook run PreToolUse"
      }
    ]
  }
}
```

Use `--from` to point at a local checkout before the package is published:

```bash
capt-hook generate-settings --from path/to/captain-hook
```

Only includes event types that have at least one registered hook. Pipe to a file or merge into your existing settings:

```bash
capt-hook --hooks src/ generate-settings > .claude/settings.local.json
```

---

## `logs`

View a recent captain-hook session log written under the cache log directory.

```bash
capt-hook logs
```

With no options it prints the most recently modified session log.

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--session` | Most recent session | Session id, or transcript path (hashed) to locate its log file |
| `--tail` | All lines | Show only the last N lines |

---

## Integration with Claude Code

The typical setup flow:

```bash
uv add capt-hook                            # add as dependency
capt-hook init                              # scaffold project
# edit .claude/hooks/my_hooks.py            # write your hooks
capt-hook test                              # verify (--hooks defaults to .claude/hooks)
capt-hook generate-settings                 # regenerate settings
```

Claude Code reads `.claude/settings.local.json` on startup and calls `uvx capt-hook` for each registered event type.
