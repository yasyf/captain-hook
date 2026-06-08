# captain-hook

[![PyPI](https://img.shields.io/pypi/v/capt-hook.svg)](https://pypi.org/project/capt-hook/)
[![Python](https://img.shields.io/pypi/pyversions/capt-hook.svg)](https://pypi.org/project/capt-hook/)
[![Docs](https://github.com/yasyf/captain-hook/actions/workflows/docs.yml/badge.svg)](https://yasyf.github.io/captain-hook/)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue.svg)](https://github.com/yasyf/captain-hook/blob/main/LICENSE)

Declarative hook framework for Claude Code. Write hooks as data, test them inline, and ship them to CI in the same shape they run in production.

## Install

There's no install step. Run everything through [uvx](https://docs.astral.sh/uv/).

```bash
uvx capt-hook init
```

`uvx` fetches captain-hook into a throwaway environment and runs it, so you never add it to `pyproject.toml`. Every command below works the same way once you prefix it with `uvx`.

## First hook

`uvx capt-hook init` scaffolds `.claude/hooks/`, wires Claude Code's settings, and installs the skills. One command and you're live.

```bash
uvx capt-hook init
```

A hook is declarative Python with an event, some conditions, and an action. This one stops the agent from finishing a UI change it never looked at.

```python
# .claude/hooks/visual_review.py
from captain_hook import gate, TouchedFile, UsedSkill

# A Stop gate: before the agent finishes, block if it edited UI files
# without doing a visual review.
gate(
    # the one-line reason shown to the agent when the gate fires
    "You edited UI files. Open them with agent-browser and verify they render before finishing.",
    only_if=[TouchedFile("**/src/routes/**", "**/src/components/**")],  # fires only if UI files changed
    skip_if=[UsedSkill("agent-browser")],                               # already reviewed -> don't block
)
```

Conditions match tools, files, commands, and even which skills the agent used.

## Test your hooks

Every deterministic hook carries inline tests, so a broken hook fails like broken code. Run them from your project root, where `--hooks` defaults to `.claude/hooks`.

```python
# .claude/hooks/safety.py
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "stash"],
    reason="Use the team's VCS workflow for shelving changes",
    hint="Commit a WIP change instead of stashing",
    tests={
        Input(command="git stash"): Block(),
        Input(command="git status"): Allow(),
    },
)
```

```bash
uvx capt-hook test
```

`init` already wired Claude Code's settings. Each event runs `uvx capt-hook run <Event>`, with the event JSON arriving on stdin and the verdict written to stdout. Re-run `uvx capt-hook generate-settings` only after you add hooks on a new event.

## Agent skills & plugin

capt-hook ships two [Agent Skills](https://yasyf.github.io/captain-hook/docs/getting-started/skills.html) so you don't have to write hooks by hand. `bootstrapping-hooks` mines your repo's docs, CI, and git history into proposed gates and nudges. `translating-styleguides` turns a STYLEGUIDE.md into enforced rules. `uvx capt-hook init` installs both into `.claude/skills/`, or you can add them as a plugin.

```
/plugin marketplace add yasyf/captain-hook
/plugin install captain-hook@captain-hook
```

## What problems does this solve?

captain-hook covers four jobs:

- Block dangerous tool calls before they execute on `PreToolUse`, like force-push, package-manager footguns, and raw `rm -rf`.
- Drive the agent with feedback that fires on the patterns it actually emits, such as repeated failures, weakened tests, and missed conventions.
- Enforce multi-step workflows with stop-gates and artifact validation, so the agent can't declare "done" without running tests, writing a report, or completing a checklist.
- Keep all of the above testable. Every hook ships with inline `tests = {...}` that `uvx capt-hook test` runs in CI, so you catch broken hooks the way you catch broken code.

## Docs

[Read the docs](https://yasyf.github.io/captain-hook/) for the full guide to conditions, primitives, LLM hooks, workflows, state, and real-world patterns.

For working on captain-hook itself, see the [development guide](https://yasyf.github.io/captain-hook/docs/development/).
