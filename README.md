# captain-hook

<!-- badges -->

Declarative hook framework for Claude Code. Write hooks as data, test them inline, and ship them to CI in the same shape they run in production.

## Install

```bash
pip install captain-hook
```

## First hook

Scaffold a project and drop a hook into `.claude/hooks/`:

```bash
uvx captain-hook init
```

```python
# .claude/hooks/my_first.py
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "stash"],
    reason="Use the team's VCS workflow for shelving changes",
    hint="Commit a WIP change instead of stashing",
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
    },
)
```

Run the inline tests:

```bash
captain-hook test --hooks .claude/hooks
```

Wire the hook into Claude Code's settings:

```bash
captain-hook generate-settings --hooks .claude/hooks > .claude/settings.local.json
```

The next time Claude tries `git stash`, captain-hook returns a deny with your reason and hint.

## What problems does this solve?

- **Block dangerous tool calls** before they execute (`PreToolUse`) — force-push, package-manager footguns, raw `rm -rf`.
- **Drive the agent with feedback** that fires on patterns it actually emits — repeated failures, weakened tests, missed conventions.
- **Enforce multi-step workflows** with stop-gates and artifact validation, so the agent can't declare "done" without running tests / writing a report / completing a checklist.
- **Keep all of the above testable** — every hook ships with inline `tests = {...}` that `captain-hook test` runs in CI, so you catch broken hooks the same way you catch broken code.

## Docs

[Read the docs](./docs/index.md) for the full guide: conditions, primitives, LLM hooks, workflows, state, and real-world patterns.
