# captain-hook

[![PyPI](https://img.shields.io/pypi/v/capt-hook.svg)](https://pypi.org/project/capt-hook/)
[![Python](https://img.shields.io/pypi/pyversions/capt-hook.svg)](https://pypi.org/project/capt-hook/)
[![Docs](https://github.com/yasyf/captain-hook/actions/workflows/docs.yml/badge.svg)](https://yasyf.github.io/captain-hook/)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue.svg)](https://github.com/yasyf/captain-hook/blob/main/LICENSE)

Declarative hook framework for Claude Code. Write hooks as data, test them inline, and ship them to CI in the same shape they run in production.

## Install

No install needed — run everything through [uvx](https://docs.astral.sh/uv/):

```bash
uvx capt-hook init
```

`uvx` fetches captain-hook into a throwaway environment and runs it, so you never add it to `pyproject.toml`. Every command below works the same way: prefix it with `uvx`.

## First hook

Scaffold a project and drop a hook into `.claude/hooks/`:

```bash
uvx capt-hook init
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

Run the inline tests (from your project root, `--hooks` defaults to `.claude/hooks`):

```bash
capt-hook test
```

Wire the hook into Claude Code's settings:

```bash
capt-hook generate-settings > .claude/settings.local.json
```

The next time Claude tries `git stash`, captain-hook returns a deny with your reason and hint.

## Agent skills & plugin

Don't want to write hooks by hand? capt-hook ships two [Agent Skills](https://yasyf.github.io/captain-hook/docs/getting-started/skills.html) — `bootstrapping-hooks` mines your repo's docs, CI, and git history into proposed gates and nudges; `translating-styleguides` turns a STYLEGUIDE.md into enforced rules. `uvx capt-hook init` installs them into `.claude/skills/`, or get them as a plugin:

```
/plugin marketplace add yasyf/captain-hook
/plugin install captain-hook@captain-hook
```

## What problems does this solve?

- **Block dangerous tool calls** before they execute (`PreToolUse`) — force-push, package-manager footguns, raw `rm -rf`.
- **Drive the agent with feedback** that fires on patterns it actually emits — repeated failures, weakened tests, missed conventions.
- **Enforce multi-step workflows** with stop-gates and artifact validation, so the agent can't declare "done" without running tests / writing a report / completing a checklist.
- **Keep all of the above testable** — every hook ships with inline `tests = {...}` that `capt-hook test` runs in CI, so you catch broken hooks the same way you catch broken code.

## Docs

[Read the docs](https://yasyf.github.io/captain-hook/) for the full guide: conditions, primitives, LLM hooks, workflows, state, and real-world patterns.

Working on captain-hook itself? See the [development guide](https://yasyf.github.io/captain-hook/docs/development/).
