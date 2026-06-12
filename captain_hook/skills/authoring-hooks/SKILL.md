---
name: authoring-hooks
description: Drafts one capt-hook (captain-hook) hook from a durable correction — the user's verbatim feedback plus its context — as a new .claude/hooks/<slug>.py, or (FIX mode) amends an existing misfiring hook with a mandatory regression test reproducing the misfire. Picks the right primitive (nudge for one-shot advice, gate for one-shot stop checks, hook(block=True) for always-on enforcement), writes the narrowest condition that captures the correction, a message that cites the correction, and inline tests (one Input firing on the offending shape, one Allow() on a benign neighbor), then proves the file with uvx capt-hook test before any settings wiring. Use when the user says "author a hook", "draft a hook from feedback", "encode this correction as a hook", "fix this misfiring hook", or when the bootstrapping-hooks or scanning-sessions skill delegates a hook to write or amend.
argument-hint: "[the correction to encode — verbatim user text + context]"
allowed-tools: Read, Grep, Glob, Write, Edit, Bash(uvx capt-hook:*, capt-hook:*, ls:*, git log:*)
---

# Authoring a Hook from a Correction

capt-hook is a declarative hook framework for Claude Code. Hooks are Python files in
`.claude/hooks/`, dispatched by `uvx capt-hook run <Event>` entries in
`.claude/settings.local.json`. Each hook carries inline tests —
`tests={Input(...): Block() | Warn() | Allow()}` — run with `uvx capt-hook test`. This
skill turns **one durable correction** (the user's verbatim feedback plus the context it
fired in) into **one new hook file** `.claude/hooks/<slug>.py`. Full API:
[capt-hook API reference](references/capt-hook-api.md).

## Hard Rules

- **Read [references/pitfalls.md](references/pitfalls.md) before picking a primitive.**
  Every rule there is a shipped failure mode, not advice.
- **`gate()`/`nudge()` are one-shot nudges, never enforcement.** An always-enforcing
  guard is `hook(..., block=True)` (or `block_command`). Never use `gate()` for
  security or correctness.
- **Narrowest condition that captures the correction.** An over-broad condition
  re-fires on unrelated calls and erodes trust; misfire complaints get mined and turned
  into fix-PRs against your hook.
- **Every deterministic hook ships inline tests** — one `Input` asserting the hook
  fires on the offending shape, one asserting it stays silent on a benign neighbor.
- **`uvx capt-hook test` must be green before any wiring.** A hook command that fails
  at dispatch blocks the user's session; wire only what is proven to run.

## Workflow

Copy this checklist into your response and check off steps as you complete them:

```
Authoring Progress:
- [ ] Step 1: Restate the correction as a rule
- [ ] Step 2: Pick the primitive (per references/pitfalls.md)
- [ ] Step 3: Write the hook — condition, message, inline tests
- [ ] Step 4: Verify (uvx capt-hook test, fix until green)
- [ ] Step 5: Wire settings (only after green, only if needed)
```

### 1. Restate the correction as a rule

From the verbatim correction and its context, extract:

- **The rule**: one sentence in "never X" / "always Y before Z" / "use A not B" form.
  If the correction names one specific line, file, or test, it is task-scoped — stop
  and say so instead of writing a hook.
- **The offending shape**: the exact tool call or content the user corrected — a
  command line, a file edit, a stop-without-testing. This becomes the firing test.
- **A benign neighbor**: the closest input that must *not* fire — the same command
  with a safe flag, the same edit in a test file, an unrelated file. This becomes the
  `Allow()` test.
- **The slug**: a short snake_case name for the rule (`no_force_push`,
  `logger_not_print`) — it names the file `.claude/hooks/<slug>.py`.

### 2. Pick the primitive

Decide enforcement first, then shape — [references/pitfalls.md](references/pitfalls.md)
has the full decision rules and defaults:

| The rule is... | Primitive |
|---|---|
| A guard that must hold on **every** occurrence (safety, correctness) | `hook(..., block=True)`; for bash commands `block_command` |
| A dangerous-command pattern, advisory | `warn_command` |
| A done-criterion to check once at stop ("run tests before stopping") | `gate(only_if=[...], skip_if=[RanCommand(...)])` |
| Advice worth surfacing once per session | `nudge` |
| A code-content rule needing AST precision | `lint()` |
| A whole style guide | delegate to the `translating-styleguides` skill |

Worked, test-passing code for each shape:
[pattern catalog](references/pattern-catalog.md).

### 3. Write the hook

Create `.claude/hooks/<slug>.py` containing exactly one registration. (When the
invoking skill names a target file instead — `bootstrapping-hooks` groups hooks by
category into `safety.py`, `quality.py`, ... — append the registration there.) Every
registration gets:

- `from __future__ import annotations` at the top.
- The narrowest condition that captures the correction: prefer
  `Command(r"...")` with anchored tokens over substrings, `FilePath`/`TestFile`
  scoping over bare `Tool`, and `skip_if` carve-outs for the benign neighbor.
  Import gotcha: the `Command` regex *condition* is `from captain_hook.types import
  Command` — top-level `captain_hook.Command` is the parsed-command class.
- The **verbatim correction quoted inside the message** with its source ("user
  feedback 2026-06-09: 'never force-push to main'") — the agent being blocked learns
  *why*.
- Inline `tests = {...}` from Step 1: the offending shape expecting `Block(...)` or
  `Warn(...)` (match the chosen severity), the benign neighbor expecting `Allow()`.
  LLM hooks (`llm_gate`, `llm_nudge`) ship without `tests=` — their inline tests would
  only exercise a stubbed model.

### 4. Verify

Run:

```bash
uvx capt-hook test
```

Add `--json` when parsing results. Fix failures until green — debugging recipes in
[testing hooks](references/testing-hooks.md). Never weaken a test to pass; fix the
hook.

### 5. Wire settings

Only after Step 4 is green, and only when the hook targets an event no existing
`.claude/settings.local.json` entry dispatches:

```bash
uvx capt-hook register-hooks
```

It merges non-destructively (add `--dry-run` to preview). If the hook's event is
already wired, there is nothing to do — the new file is picked up on the next
dispatch.

## Worked mini-example

Correction received (verbatim): *"stop using pip — this repo is uv-only, you've done
this three times now"*, given right after `pip install requests` ran.

- Rule: use uv, not pip. Offending shape: `pip install requests`. Benign neighbor:
  `uv add requests`. Slug: `uv_not_pip`. Primitive: repeated tool-substitution
  correction, advisory → `warn_command`.

`.claude/hooks/uv_not_pip.py`:

```python
from __future__ import annotations

from captain_hook import Allow, Input, Warn, warn_command

warn_command(
    ["pip", "install"],
    message="User feedback: 'stop using pip -- this repo is uv-only'. Run `uv add <pkg>`.",
    tests={
        Input(command="pip install requests"): Warn(pattern="uv-only"),
        Input(command="uv add requests"): Allow(),
    },
)
```

`uvx capt-hook test` → 2 passed; `PostToolUse` is already wired, so no Step 5.

## FIX mode — amending a misfiring hook

When the input is a **misfire complaint** instead of a correction — the scanning-sessions
skill hands you a fix candidate carrying the target hook file, the hook's registered
name, the misfire class, and Claude's verbatim complaint — you **amend the existing
hook file**, never write a new one.

### 1. Reproduce the misfire

From the complaint and its context, extract the **offending input**: the exact tool
call or content the hook wrongly fired on (for a re-fire, the repeat occurrence the
hook should have stayed silent on). Also extract the **genuine case**: the input the
hook exists to catch — read it off the hook's current inline tests and message. If you
cannot state the offending input precisely, stop and report the candidate as
unreproducible instead of guessing.

### 2. Pick the narrowest amendment

In order of preference:

| Misfire shape | Amendment |
|---|---|
| The condition matches calls outside the rule's intent | **Tighten the condition** — anchor the regex, scope with `FilePath`/`TestFile`, add a `skip_if` carve-out |
| The hook re-fires on content it already fired on (`max_fires` too high, no per-turn guard) | **Add a re-fire guard** — lower `max_fires`, or `skip_if` on the already-satisfied state |
| The hook re-fires because it greps stale transcript text instead of live state | **Switch to live state** — read the event object (`evt.tasks`, `evt.ctx`) instead of transcript text |
| The rule is real but blocking is disproportionate | **Demote `block=True` → `Warn`** (or `block_command` → `warn_command`) |
| The rule no longer holds at all | **Remove the registration** (and say so in the PR body) |

### 3. Write the regression test — MANDATORY

Every fix ships a regression test reproducing the misfire inside the hook's
`tests = {...}`:

- one `Input(...)` built from the **offending input**, asserting the amended hook
  stays silent: `Allow()`;
- one `Input(...)` for the **genuine case**, asserting the hook still fires
  (`Block(...)`/`Warn(...)` matching its severity).

A fix without the silent-on-misfire test is not done — that test is what stops the
same complaint from being mined again next session.

### 4. Verify

`uvx capt-hook test` must be green, existing tests included. Never delete or weaken
the hook's existing tests to make the amendment pass; if the genuine-case test now
fails, the amendment is too broad — go back to Step 2. No settings wiring changes:
the file is already dispatched.

## References

- [capt-hook API reference](references/capt-hook-api.md) — events, primitives, conditions, event object, CLI.
- [Pattern catalog](references/pattern-catalog.md) — one validated hook file per taxonomy category.
- [Testing hooks](references/testing-hooks.md) — inline test format, fixtures, debugging recipes.
- [Pitfalls](references/pitfalls.md) — primitive-choice and wiring failure modes; read before Step 2.
