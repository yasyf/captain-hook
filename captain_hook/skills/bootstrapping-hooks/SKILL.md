---
name: bootstrapping-hooks
description: Surveys a repository and sets up captain-hook (capt-hook) guardrails for Claude Code — blocking gates, advisory nudges, command blocks, and test-integrity checks mined from the repo's own docs, CI workflows, lint configs, and git history. Scaffolds the framework and enables the session reviewer up front (Step 1), then proposes categorized candidates for user approval before writing anything, then writes .claude/hooks/*.py with inline tests, verifies with capt-hook test, and wires .claude/settings.local.json. Use when the user asks to "set up captain hook", "set up capt-hook", "set up hooks", "bootstrap capt-hook", "add guardrails", "enforce our conventions with hooks", "protect this repo", or "make Claude follow CONTRIBUTING.md".
argument-hint: "[repo path] (defaults to current project)"
allowed-tools: Read, Grep, Glob, AskUserQuestion, Write, Edit, Bash(uvx capt-hook:*, capt-hook:*, git log:*, git diff:*, ls:*, find:*)
---

# Bootstrapping capt-hook Guardrails

capt-hook is a declarative hook framework for Claude Code. Hooks are Python files in
`.claude/hooks/`, dispatched by `uvx capt-hook run <Event>` entries in
`.claude/settings.local.json`. Each hook carries inline tests —
`tests={Input(...): Block() | Warn() | Allow()}` — run with `uvx capt-hook test`. Hooks are
always Python regardless of the target repo's language: conditions like `Command` and
`FilePath` are language-agnostic; only AST `lint` rules are Python-specific. The full
API reference, pattern catalog, and testing guide ship with the `authoring-hooks`
skill, which owns hook drafting (Step 6 delegates to it).

## Hard Rules

- **Never write a hook the user has not approved in Step 4.** Survey and propose first; write only what was selected.
- **Every deterministic hook ships inline tests** — at least one firing `Input` and one `Allow()` — and `uvx capt-hook test` must be green before Step 8. LLM hooks (`llm_gate`, `llm_nudge`, `prompt_check`) and signal-scored `nudge`s ship without `tests=`: their inline tests would only exercise a stubbed model.
- **Propose `block` only for irreversible or destructive actions** (history rewrites, data deletion, deploys, secret leaks). Default everything else to warn. The user picks final severity in Step 4.
- **Never write style rules here.** A style guide found during the survey is delegated whole to the `translating-styleguides` skill (category E below).

## Workflow

Copy this checklist into your response and check off steps as you complete them:

```
Bootstrap Progress:
- [ ] Step 1: Locate + scaffold (init or review enable) + pre-flight
- [ ] Step 2: Survey the repo (docs, CI, lint configs, git log)
- [ ] Step 3: Mine candidates onto the taxonomy
- [ ] Step 4: Propose via AskUserQuestion — nothing written before approval
- [ ] Step 5: Clear the demo example.py (scaffolding ran in Step 1)
- [ ] Step 6: Draft approved hooks via authoring-hooks, one file per category
- [ ] Step 7: Verify (uvx capt-hook test, fix until green)
- [ ] Step 8: Wire settings (register-hooks if new events)
- [ ] Step 9: Final report (table + declined list)
```

### 1. Locate + scaffold + pre-flight

Resolve the target repo (argument path, else current project). Inspect what's already wired:

```bash
ls .claude/hooks/ 2>/dev/null
grep -lq 'capt-hook' .claude/settings.json 2>/dev/null && echo COMMITTED || echo FRESH
```

Then scaffold up front, so the framework and the session reviewer are live before you propose
anything — pick the command by what you found:

- **FRESH** (no committed capt-hook wiring) — run `uvx capt-hook init`. It scaffolds
  `.claude/hooks/`, wires `.claude/settings.local.json`, installs the skills, and **enables the
  session reviewer** (watching this repo; it mines ended sessions and opens hook PRs —
  `uvx capt-hook review disable` to stop).
- **COMMITTED** (a checked-in `.claude/settings.json` already runs `uvx capt-hook run …`) — do
  **not** run `init`; it would duplicate those hooks into `settings.local.json` and double-fire.
  Run `uvx capt-hook review enable` instead — it installs the reviewer skills and arms the session
  reviewer without touching the committed event hooks.

Read `.claude/settings.local.json` and `.claude/settings.json`. If capt-hook hooks already exist,
switch to **additive mode**: never overwrite existing hook files; new categories go in new files,
and the Step 4 menu only offers candidates not already covered.

### 2. Survey the repo

Read, in order of signal density:

1. `CONTRIBUTING.md`, `AGENTS.md`, `CLAUDE.md`, `STYLEGUIDE.md` (and `docs/` equivalents), `README.md` — explicit rules ("never X", "always Y before Z", "use A not B").
2. `.github/workflows/*.yml` — the exact test/lint job commands become gate skip-conditions.
3. Lint configs — `pyproject.toml [tool.ruff]`, `.ruff.toml`, `.eslintrc*`, `biome.json`, `.golangci.yml`, `.pre-commit-config.yaml`.
4. Task runners — `Makefile`, `justfile`, `package.json` scripts — to learn the *exact* test/lint commands the repo uses.
5. Incident archaeology. Run:

```bash
git log --oneline -50
git log -i --grep="revert\|undo\|accidentally" --oneline -20
```

Repeated "fix lint" commits suggest a lint gate; a revert of a force-push suggests a command block.

### 3. Mine candidates

Map every signal onto the taxonomy below. Record per candidate: the **source quote**
("CONTRIBUTING.md: never force-push to main"), the **category**, the **primitive**, and a
**proposed severity**. Drop nothing silently — weak candidates go in the menu marked as such.

| Category | Repo signals | Primitive |
|---|---|---|
| A. Command safety | "never run X", destructive ops (`rm -rf`, db reset, deploy, force-push), tool substitutions ("use uv not pip") in docs; reverts in git log | `block_command` / `warn_command`; `@on` + `evt.command_line.q` for compound commands (curl-pipe-sh) |
| B. Code quality | lint configs, "use logger not print", banned imports/idioms in docs | `hook(only_if=[Content(...)])`, `lint()`, `nudge(signals=...)`, `llm_gate` escalation |
| C. Test integrity | `tests/` dir + CI test job; "never skip tests", coverage rules | `gate(only_if=[TouchedFile], skip_if=[RanCommand])`; `prompt_check` on test-file edits |
| D. Workflow rituals | CONTRIBUTING rituals ("run make lint before pushing", "update CHANGELOG"), multi-step done-criteria | `gate` on `PreToolUse` + `Command(r"git\s+push")`; `workflow()` for ordered checklists |
| E. Styleguide rules | `STYLEGUIDE.md`, style sections in CONTRIBUTING/AGENTS/CLAUDE | **delegate to `translating-styleguides`** |

Worked code per category: the pattern catalog in the `authoring-hooks` skill
(`references/pattern-catalog.md` there).

### 4. Propose via AskUserQuestion

One question per non-empty category (batch up to 4 per AskUserQuestion call), with
`multiSelect: true`. Each option is one concrete hook; its description carries the source quote
and the proposed primitive + severity. Then one final severity question:

- "Block all approved gates"
- "Warn-only everywhere"
- "Decide per hook (use the proposed severities)"

If a styleguide-like markdown was found, category E is a single option: **"Translate `<file>`
into enforced style rules (runs the translating-styleguides skill)"**.

### 5. Clear the demo example.py

Scaffolding already ran in Step 1. If that `init` created the demo `.claude/hooks/example.py`,
delete it once you've drafted the real hooks (Step 6) — the approved hooks replace it. (In
**COMMITTED** repos `review enable` writes no `example.py`, so there's nothing to clear.)

### 6. Write hooks

One file per approved category: `safety.py`, `quality.py`, `testing.py`, `workflow.py`
(+ `style.py`, owned end-to-end by `translating-styleguides`). Drafting is delegated:
for each approved hook, invoke the `authoring-hooks` skill via the Skill tool, passing

- the **source quote, verbatim** (it becomes the citation inside the message — the
  agent being blocked learns *why*),
- the approved **primitive and severity** from Step 4,
- the surveyed repo's *exact* commands (e.g. `make test` vs `uv run pytest` vs
  `npm test`) for messages and `RanCommand` regexes,
- the **target category file** so related hooks stay grouped.

`authoring-hooks` owns the rest — primitive-choice pitfalls, the narrowest matching
condition, inline tests (at least one firing `Input` and one `Allow()`), and the
pattern catalog its drafts copy from. If the Skill tool is unavailable, read that
skill's `SKILL.md` directly and follow it — both skills ship together.

### 7. Verify

Run:

```bash
uvx capt-hook test
```

Add `--json` when parsing results (one JSON record per test). Fix failures until green —
debugging recipes in the `authoring-hooks` skill's `references/testing-hooks.md`. Never
weaken a test to pass; fix the hook.

### 8. Wire settings

Required whenever hooks target events `init` didn't know about (e.g. a new `Stop` gate added
after scaffolding). Run:

```bash
uvx capt-hook register-hooks
```

`register-hooks` writes `.claude/settings.local.json` directly, merging non-destructively: it
preserves every non-captain-hook entry, refreshes captain-hook's own, and drops entries for
events you no longer subscribe to. Add `--dry-run` to print the merged JSON without writing.

### 9. Final report

Output a markdown table plus a declined list:

```
| hook                    | file        | primitive     | severity | source                       | tests |
|-------------------------|-------------|---------------|----------|------------------------------|-------|
| no-force-push           | safety.py   | block_command | block    | CONTRIBUTING.md "never ..."  | 3     |
| tests-before-stop       | testing.py  | gate          | block    | CI: uv run pytest            | 3     |

Declined: <candidates the user rejected, with their source quotes>
```

Close with next steps: `uvx capt-hook logs --tail 50` to inspect live firings, and tune
`max_fires` on any hook that nags. Note that Step 1 also armed the **session reviewer** — it now
watches this repo, mines your ended sessions for durable corrections, and opens hook PRs
automatically; `uvx capt-hook review disable` turns it off.

## Worked mini-example

Survey of a fictional repo finds two signals:

- CONTRIBUTING.md: "always run `make lint` before pushing"
- README.md, Setup: "this repo uses uv, not pip"

Step 4 proposes two options — category D: *lint-before-push gate (block)*, category A:
*warn on pip install (warn)*. The user approves both; each is drafted by the
`authoring-hooks` skill. The pip warning goes in `safety.py` (pattern catalog,
category A). The gate goes in `.claude/hooks/workflow.py`:

```python
from __future__ import annotations

from captain_hook import Allow, Block, Event, Input, RanCommand, Tool, gate
from captain_hook.types import Command

gate(
    "CONTRIBUTING.md requires `make lint` before pushing.",
    events=Event.PreToolUse,
    only_if=[Tool("Bash"), Command(r"git\s+push")],
    skip_if=[RanCommand(r"make\s+lint")],
    tests={
        Input(command="git push origin main"): Block(pattern="make lint"),
        Input(command="git status"): Allow(),
    },
)
```

`uvx capt-hook test` confirms:

```
  PASS  workflow:gate_50b992e3:Input(command='git push origin main', ...)
  PASS  workflow:gate_50b992e3:Input(command='git status', ...)

2 tests: 2 passed, 0 failed, 0 errors, 0 skipped
```

Report row:

```
| lint-before-push | workflow.py | gate | block | CONTRIBUTING.md "always run make lint before pushing" | 2 |
```

## Delegating style guides

When the survey finds a style guide (`STYLEGUIDE.md`, a "Code style" section in
CONTRIBUTING/AGENTS/CLAUDE, `docs/style*.md`), this skill **never** writes `StyleRule`s
itself. If the user approves the category E option, invoke the `translating-styleguides`
skill via the Skill tool with the markdown path as args; it owns `style.py` end-to-end and
its enforcement report is appended to this skill's final report. If the Skill tool is
unavailable, read that skill's `SKILL.md` directly and follow it — both skills ship together.

## References

The capt-hook API reference, pattern catalog, and testing guide live in the
`authoring-hooks` skill's `references/` directory — Step 6 delegates drafting there, so
this skill carries none of its own.
