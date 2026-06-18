# captain-hook

![captain-hook banner](https://github.com/yasyf/captain-hook/raw/main/docs/assets/readme-banner.webp)

[![PyPI](https://img.shields.io/pypi/v/capt-hook.svg)](https://pypi.org/project/capt-hook/)
[![Python](https://img.shields.io/pypi/pyversions/capt-hook.svg)](https://pypi.org/project/capt-hook/)
[![Docs](https://github.com/yasyf/captain-hook/actions/workflows/docs.yml/badge.svg)](https://yasyf.github.io/captain-hook/)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue.svg)](https://github.com/yasyf/captain-hook/blob/main/LICENSE)

Guardrails for Claude Code, written as typed, testable data — and learned from the corrections you give Claude.

A captain-hook hook is declarative Python: an event, some conditions, an action. Block a footgun before it runs, nudge the agent off a bad pattern, gate "done" until the tests pass. Then captain-hook closes the loop: it reads the corrections you give Claude as you work and opens pull requests that codify the durable ones as new hooks. You write the first few; it writes the rest.

## Install

captain-hook needs no install — it runs through [uvx](https://docs.astral.sh/uv/). From your project root:

```bash
uvx capt-hook init
```

`init` scaffolds `.claude/hooks/`, wires Claude Code's settings, registers the captain-hook plugin so its skills install on workspace-trust, and arms the [session reviewer](#it-learns-from-your-corrections). Or do it all from a session. Run `/plugin marketplace add yasyf/captain-hook`, then ask Claude to "set up captain hook".

## Your first hook

A hook is an event, some conditions, and an action. This one stops the agent from finishing a UI change it never looked at:

```python
# .claude/hooks/visual_review.py
from captain_hook import gate, TouchedFile, UsedSkill

gate(
    "You edited UI files. Open them with agent-browser and verify they render before finishing.",
    only_if=[TouchedFile("**/src/routes/**", "**/src/components/**")],
    skip_if=[UsedSkill("agent-browser")],
)
```

`only_if` arms the gate only when UI files changed; `skip_if` stands it down once the agent has done the review. Conditions match tools, files, commands, and even which skills the agent used.

## It learns from your corrections

Most hooks you'll never write by hand.

The corrections you give Claude as you work are exactly the rules a hook should enforce: "never force-push", "use `uv`, not `pip`", "you weakened that test". Writing the hook by hand is friction you skip in the moment, so the **session reviewer** notices for you. When a session ends, it reads the transcript, finds the durable corrections and the hooks that misfired, judges which ones are standing rules and which are one-offs, and once a pattern proves itself across sessions, opens a pull request that adds the hook — or fixes the one that misfired. You review the PR like any other.

It's on by default after `init`. Turn it off for a repo with `uvx capt-hook review disable`. The [session reviewer guide](https://yasyf.github.io/captain-hook/docs/guide/session-reviewer.html) covers the prerequisites (an authenticated `claude` and `gh`) and the `HOOKS_REVIEW_*` thresholds.

## Tested like code

Every deterministic hook carries inline tests, so a broken hook fails like broken code:

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

Run them from your project root, where `--hooks` defaults to `.claude/hooks`:

```bash
uvx capt-hook test
```

Wire that into CI and you catch a broken hook the way you catch broken code.

## What it's for

- Block footguns before they run on `PreToolUse`: force-push, `rm -rf`, package-manager traps.
- Steer the agent with feedback that fires on the patterns it actually emits: repeated failures, weakened tests, missed conventions.
- Hold the line on multi-step work with Stop gates and artifact checks, so the agent can't call it "done" before the tests run or the report's written.
- Keep all of it testable; every hook ships with inline tests that run in CI.

## Docs

[Read the docs](https://yasyf.github.io/captain-hook/) for the full guide to conditions, primitives, LLM hooks, workflows, state, and real-world patterns. To work on captain-hook itself, see the [development guide](https://yasyf.github.io/captain-hook/docs/development/).

## License

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE), free for noncommercial use.
