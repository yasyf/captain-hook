# ![captain-hook](https://github.com/yasyf/captain-hook/raw/main/docs/assets/readme-banner.webp)

**Stop repeating yourself to Claude.** captain-hook mines your transcripts for the corrections you keep giving and opens PRs that turn each one into a typed, tested Python hook.

[![CI](https://github.com/yasyf/captain-hook/actions/workflows/ci.yml/badge.svg)](https://github.com/yasyf/captain-hook/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/capt-hook)](https://pypi.org/project/capt-hook/)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm--Noncommercial--1.0.0-blue)](https://github.com/yasyf/captain-hook/blob/main/LICENSE)

## Get started

```bash
brew install --formula yasyf/tap/captain-hook
uvx capt-hook helper install
uvx capt-hook init
```

The formula fills the Cellar and `helper install` deploys the fixed, signed host at
`~/Applications/Captain Hook.app`; `init` scaffolds
`.claude/hooks/`, wires Claude Code's settings, and arms the session reviewer. Every event runs
the exact `capt-hook` build the installed app names — nothing resolves "latest" mid-session, and
the app keeps itself current in the background. One `block_command` later, a force-push dies
at `PreToolUse` and the hook's inline tests run green:

<img src="https://github.com/yasyf/captain-hook/raw/main/docs/assets/demo.gif" alt="Animated terminal: a hook blocks git push --force at PreToolUse, then 'uvx capt-hook test' passes both inline tests" width="700">

Driving with an agent? Paste this:

```text
/plugin marketplace add yasyf/captain-hook
/plugin install captain-hook@captain-hook
```

<details>
<summary>Prefer a prompt over the plugin?</summary>

```text
Install the `yasyf/tap/captain-hook` Homebrew formula, run `uvx capt-hook helper install`,
then run `uvx capt-hook init` in this repo, write one hook that blocks force-pushes,
and verify it with `uvx capt-hook test`. Read https://yasyf.github.io/captain-hook/
if you get stuck.
```

</details>

---

## Use cases

### Block force-push and rm -rf before they run

One bad Bash call rewrites shared history or eats a directory, and by the time you spot it in the transcript it already ran. Declare the block once, tests inline:

```python
# .claude/hooks/safety.py
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "push", "--force"],
    reason="Force-pushing rewrites shared history",
    hint="Use `git push --force-with-lease` instead",
    tests={
        Input(command="git push --force"): Block(),
        Input(command="git push origin main"): Allow(),
    },
)
```

The next `git push --force` never executes: the agent sees `BLOCKED: Force-pushing rewrites shared history` plus the hint, and reaches for `--force-with-lease` instead. And when a pattern can't decide, walk the parse — this is the heart of the shipped `rm` guard:

```python
for call in evt.command.calls("rm"):
    if call.targets.expand().exhausted:
        return evt.block("rm targets too broad to verify")
    return call.sub("rm", "trash", args=call.targets)
```

`evt.command` is the parsed command line: every `rm` across `&&` and pipes, each target resolved against the working directory, the rewrite quote-safe.

### Turn repeated corrections into rules Claude can't forget

You've typed "use uv, not pip" in a dozen sessions, and session thirteen makes the same mistake. After `init`, the session reviewer reads each transcript as the session ends, keeps the corrections that are standing rules, and — once a pattern proves itself across sessions — opens a PR that codifies it as a hook. Watch the pipeline:

```bash
uvx capt-hook status
```

The dashboard lists every correction it's tracking, staged from first sighting to open PR. You review the PR like any other; merged hooks enforce the rule from then on.

### Gate "done" until the tests actually pass

The agent declares victory while the suite is red. A Stop gate holds the line:

```python
# .claude/hooks/quality.py
from captain_hook import RanCommand, TouchedFile, gate

gate(
    "You edited Python files but never ran the tests. Run `uv run pytest` before finishing.",
    only_if=[TouchedFile("**/*.py")],
    skip_if=[RanCommand(r"\bpytest\b")],
)
```

The agent can't end the turn until a pytest run shows up in the transcript, and the gate stands down on its own once one does.

## More in the docs

- **Interactive tutorial** — block your first command in the browser, verified against the real engine — [start it](https://yasyf.github.io/captain-hook/docs/tutorial/index.html)
- **Session reviewer** — the full corrections lifecycle, from transcript to merged hook PR — [guide](https://yasyf.github.io/captain-hook/docs/guide/session-reviewer.html)
- **Conditions** — typed filters over tools, files, commands, and transcript history — [guide](https://yasyf.github.io/captain-hook/docs/guide/primitives.html#filter-with-conditions)
- **LLM hooks** — gate on a model's verdict when a regex can't decide — [guide](https://yasyf.github.io/captain-hook/docs/guide/llm-hooks.html)
- **Workflows** — multi-step Stop gates with artifact checks and checklists — [guide](https://yasyf.github.io/captain-hook/docs/guide/state.html#enforce-a-multi-step-workflow)
- **Packs** — the shipped `general`, `python`, and `go` hook packs — [guide](https://yasyf.github.io/captain-hook/docs/guide/packs.html)
- **Testing** — run `uvx capt-hook test --json` in CI so a regressed hook fails the build — [guide](https://yasyf.github.io/captain-hook/docs/guide/testing.html)

Read the [docs](https://yasyf.github.io/captain-hook/) for the full guide. Licensed under [PolyForm Noncommercial 1.0.0](https://github.com/yasyf/captain-hook/blob/main/LICENSE), free for noncommercial use.
