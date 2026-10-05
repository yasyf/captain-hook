---
name: authoring-hooks
description: Drafts one capt-hook (captain-hook) hook from a durable correction — the user's verbatim feedback plus its context — as a new .claude/hooks/<slug>.py, or (FIX mode) amends a misfiring hook with a regression test reproducing the misfire, or (EXTEND mode) broadens a hook to a newly mined rule without weakening its tests. Picks the primitive (nudge for one-shot advice, gate for one-shot stop checks, hook(block=True) for enforcement), writes the narrowest condition, a message that states the rule then the remediation, and inline tests (one Input that fires, one Allow() on a benign neighbor), then proves the file with uvx --isolated capt-hook lint and test. Every hook change in any repo or plugin pack goes through this skill. Use when the user says "author a hook", "write a hook", "clean up this hook", "encode this correction as a hook", "fix this misfiring hook", "broaden this hook", or when bootstrapping-hooks or scanning-sessions delegates a hook.
argument-hint: "[the correction to encode — verbatim user text + context]"
allowed-tools: Read, Grep, Glob, Write, Edit, Bash(uvx capt-hook:*, uvx --isolated capt-hook:*, capt-hook:*, ls:*, git log:*)
---

# Authoring a Hook from a Correction

capt-hook is a declarative hook framework for Claude Code. Hooks are Python files in
`.claude/hooks/`, dispatched by the `bin/hook run <Event>` entries the captain-hook plugin
registers for every event. Each hook carries inline tests —
`tests={Input(...): Block() | Warn() | Allow()}` — run with `uvx --isolated capt-hook test`. This
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
- Compose shared predicates before writing a handler. Read the command and target
  APIs in the [API reference](references/capt-hook-api.md). If an operation is missing,
  extend the reusable schema or predicate; keep the hook as a declaration of the rule.
- **Every deterministic hook ships inline tests** — one `Input` asserting the hook
  fires on the offending shape, one asserting it stays silent on a benign neighbor.
- **`uvx --isolated capt-hook test` must be green before the hook goes live.** Every event is
  already registered, so a hook file that fails at dispatch blocks the user's session;
  ship only what is proven to run. `test` and `pack test` also run `capt-hook lint` over
  the hook files, so a message or code shape that misses the bar below fails the run.
- **Every hook change goes through this skill**, whoever writes it and whichever repo or
  plugin pack it lands in. The finish step is the lint, and a hook that skips it fails
  the repo's `capt-hook test` or `pack test` in CI.

## The Bar

Copy both checklists into your response and tick each item for every message and
registration you write or touch.

```
Copy bar (every message, reason, hint, and rewrite note):
- [ ] At most two sentences: the rule, then the remediation
- [ ] At most 300 characters
- [ ] Verbatim data the hook must deliver goes in a fenced block exempt from the bar; everything outside the fence still meets it
- [ ] A block names the exact command or action to run instead; a nudge names the one verb to run
- [ ] Commands, paths, and flags sit in `backticks`
- [ ] No quoted user or owner messages, and no "User feedback <date>: '...'" citations
- [ ] No ruling, inbox, record, PR, issue, session, or commit ids (R624, ruling 8B, #24918, 37768ef4)
- [ ] No dates, times, token counts, or narrative (first seen, has not replied, what went wrong last time)
- [ ] No interpolated prompt text or LLM-judge `{reasoning}`
- [ ] A time that cannot be avoided is Pacific

Code bar (every registration):
- [ ] One hook enforces one rule
- [ ] A declarative primitive (rewrite_command, block_command, warn_command, nudge, gate,
      llm_gate, llm_nudge, lint, approve, deny) where one fits; `@on` only for runtime logic
- [ ] Commands matched structurally (Runs(...), evt.command.q, an ast-grep rewrite_command
      pattern); never a regex or shlex.split over the raw command line
- [ ] Session history read through evt.ctx.t, RanCommand, UsedTool, UsedSkill, ReadFile,
      or TouchedFile; never by opening the transcript file
- [ ] No try/except fallbacks or broad `except`; the dispatcher records a raising hook's fault
- [ ] Zero comments; `TODO` and `WORKAROUND:` are the only exceptions
- [ ] Inline tests: one input that fires, one benign neighbor that stays silent
- [ ] Escapes and intent read through `Annotated(...)` or `evt.annotations`; never by parsing
      `# ccx:raw`, `tooling-lane:`, or `CAPT_HOOK_CCX_RAW` by hand
- [ ] Messages offer only the live escapes: `# ccx:raw`, `CAPT_HOOK_CCX_RAW=1`, or a `ccx:` line
- [ ] A blocking hook names its escape: `skip_if=[Annotated(<key>)]`, `confirm=Confirm(rule=...)`,
      or both; only security, merge, and session guards block with neither
```

### Escape and intent annotations

One notation carries every escape hatch and intent marker. `evt.annotations` maps `ccx:<key>[=<value>]`
tokens from three carriers: a real comment on a Bash command (`gt submit  # ccx:raw`; quoted text and
heredoc bodies never count), a whole `ccx: key=value ...` line in an Agent or Task prompt or in Skill
args, and `CAPT_HOOK_CCX_RAW` set to `1`, `true`, or `yes` for `raw`; any other value, `0` included,
contributes nothing. Keys in use: `raw` (run the command as written), `tooling-lane=<key>` (this
dispatch is the tooling lane for `<key>`), and
`role=<fix|helper|reader|watch|export|evidence|handoff|comms|triage>` (a lane's job).

`# ccx:raw` is the single command escape. `# root:raw` and the bare `tooling-lane: <key>` prompt line
are retired; a hook neither honors nor offers them.

- Skip on an annotation with `skip_if=[Annotated("raw")]`. `Annotated("role", "evidence",
  scope="session")` reads only the session's dispatch prompt, so a command can never claim a role.
- Route a block that tends to misfire through a small model with `confirm=Confirm(rule="<the one
  sentence the block protects>")` on `hook(...)` or `evt.block(...)`. The block lands only when the
  model confidently confirms the match; otherwise the call runs with a one-line note.
- `capt-hook lint` flags hand-parsed escapes, a message that offers a retired escape or a
  `CAPT_HOOK_CCX_RAW` value other than `1`, `true`, or `yes`, and a blocking hook on Agent, Task,
  Skill, Read, Grep, or Glob that carries neither an `Annotated` escape nor `confirm=`.

The history behind a rule (who asked for it, when, after which incident) belongs in the
PR body and the commit message. The agent reading a hook message needs the rule and the
next action, nothing else.

## Workflow

Copy this checklist into your response and check off steps as you complete them:

```
Authoring Progress:
- [ ] Step 1: Restate the correction as a rule
- [ ] Step 2: Pick the primitive (per references/pitfalls.md)
- [ ] Step 3: Write the hook — condition, message, inline tests
- [ ] Step 4: Verify (uvx --isolated capt-hook lint, then test, fix until clean and green)
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
| A guard that must hold on **every** occurrence (safety, correctness) | `hook(..., block=True)` with structural command and target predicates; `Runs(...)` matches an argv prefix. Use `block_command` for textual conditions. |
| A command rule, advisory | `hook(Event.PostToolUse, only_if=[Runs(...)], ...)`; `warn_command` for textual conditions |
| A done-criterion to check once at stop ("run tests before stopping") | `gate(only_if=[...], skip_if=[RanCommand(...)])` |
| Advice worth surfacing once per session | `nudge` |
| A code-content rule needing AST precision | `lint()` |
| A whole style guide | delegate to the `captain-hook:translating-styleguides` skill |

Worked, test-passing code for each shape:
[pattern catalog](references/pattern-catalog.md).

### 3. Write the hook

Create `.claude/hooks/<slug>.py` containing exactly one registration. (When the
invoking skill names a target file instead — `bootstrapping-hooks` groups hooks by
category into `safety.py`, `quality.py`, ... — append the registration there.) Every
registration gets:

- `from __future__ import annotations` at the top.
- Structural conditions for commands: use `Runs(...)` for argv prefixes. For typed
  options or named operand roles, use a `CommandSchema` and compose predicates over
  its bound targets. Keep command grammar in the schema and policy in the hook.
- Shared path predicates for command targets, preserving the parser's `Word`
  provenance for quoting and expansion. Extend a missing shared operation instead
  of looping over argv, parsing flags, or replacing `$HOME` strings inside a hook.
- `FilePath`/`TestFile` scoping for file edits, with `skip_if` for the benign neighbor.
  Reserve regexes for textual conditions. The regex condition is
  `from captain_hook.types import Command`; top-level `captain_hook.Command` is the
  parsed-command class.
- A **message that meets the copy bar**: the rule in one sentence, then the remediation
  naming the exact command or action. The verbatim correction goes in the PR body.
- Inline `tests = {...}` from Step 1: the offending shape expecting `Block(...)` or
  `Warn(...)` (match the chosen severity), the benign neighbor expecting `Allow()`.
  LLM hooks (`llm_gate`, `llm_nudge`) ship without `tests=` — their inline tests would
  only exercise a stubbed model. Exception: with a required `contexts=` provider, ship
  `tests=`, and use `Input(llm={"fire": False})` (or `{"block": False}`) to wire-test the
  judge-declines path; the default stub still always fires.

### 4. Verify

Run:

```bash
uvx --isolated capt-hook lint .claude/hooks/<slug>.py
uvx --isolated capt-hook test
```

A hook in a plugin pack runs `uvx --isolated capt-hook pack test <plugin root>` instead of
`test`. Each lint finding names its rule: rewrite the message to the copy bar, or replace
the hand-rolled code with the primitive the finding names. Add `--json` when parsing
results. Fix failures until green — debugging recipes in
[testing hooks](references/testing-hooks.md). Never weaken a test to pass; fix the
hook.

Lint clean and tests green is the finish line. The captain-hook plugin already registers every event, so
the new file is picked up on the next session — there is no settings step, whatever
event the hook targets.

## Worked mini-example

Correction received (verbatim): *"stop using pip — this repo is uv-only, you've done
this three times now"*, given right after `pip install requests` ran.

- Rule: use uv, not pip. Offending shape: `pip install requests`. Benign neighbor:
  `uv add requests`. Slug: `uv_not_pip`. Primitive: repeated tool-substitution
  correction, advisory: `hook(Event.PostToolUse, only_if=[Runs(...)], ...)`.

`.claude/hooks/uv_not_pip.py`:

```python
from __future__ import annotations

from captain_hook import Allow, Event, Input, Runs, Warn, hook

hook(
    Event.PostToolUse,
    only_if=[Runs("pip", "install")],
    message="This repo installs packages with uv. Run `uv add <pkg>`.",
    tests={
        Input(command="pip install requests"): Warn(pattern="uv add"),
        Input(command="uv add requests"): Allow(),
    },
)
```

`uvx --isolated capt-hook lint` is clean and `uvx --isolated capt-hook test` passes 2; the hook is
live from the next session — nothing to wire.

## FIX mode — amending a misfiring hook

When the input is a **misfire complaint** instead of a correction — the scanning-sessions
skill hands you a fix candidate carrying the target hook file, the hook's registered
name, the misfire class, and Claude's verbatim complaint — you **amend the existing
hook file**, never write a new one.

A pack hook is amended in the **pack's own repo**: the invoking skill hands you a
clone and the hook file's path there (`captain_hook/builtin_packs/<pack>/hooks/…` for a builtin
pack), never a copy under the watched repo's `.claude/hooks/`. Keep the hook's message
string **byte-identical** unless the amendment is the message itself or the message misses
the copy bar — fire history and complaint attribution key on a hash of the message, so a
reword orphans both, but a message that fails the lint is rewritten in the same change. The
regression matrix lives inline on the hook: the amended file's `tests = {...}` is
where the misfire and genuine-case pairs go, never a separate test file.

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
| The condition matches calls outside the rule's intent | **Tighten the condition** — use structural command or target predicates, scope with `FilePath`/`TestFile`, add a `skip_if` carve-out |
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

`uvx --isolated capt-hook lint` must be clean and `uvx --isolated capt-hook test` green, existing tests included. Never delete or weaken
the hook's existing tests to make the amendment pass; if the genuine-case test now
fails, the amendment is too broad — go back to Step 2. No settings wiring changes:
the file is already dispatched.

## EXTEND mode — broadening an existing hook

When the mined rule belongs inside a hook that already exists — the scanning-sessions
skill's overlap check matched an active hook whose stated intent the rule broadens —
you **amend that hook file**, never write a new one. The invoking skill hands you the
target hook file (in the watched repo's worktree, or a pack repo's clone for a pack
hook), the mined rule, and the verbatim correction.

- Extract per Step 1 of a create — rule sentence, offending shape, benign neighbor —
  except the offending shape is the case the hook currently **misses**.
- FIX mode's location and identity rules apply unchanged: a pack hook is amended in
  the pack's own repo, and the message string stays **byte-identical** unless the
  broadening is the message itself or the message misses the copy bar.
- Tests, inside the hook's `tests = {...}`: one `Input` built from the newly covered
  shape, asserting the hook now fires (`Block(...)`/`Warn(...)` matching its
  severity); one `Allow()` on a benign neighbor of the new case. Every pre-existing
  test stays untouched and green — an existing test failing means the broadening is
  too broad; narrow the condition, never weaken a test.
- `uvx --isolated capt-hook test` green is the finish line, existing tests included
  (in a pack clone, the invoking skill verifies with the pack-kind command from its
  PR workflow instead).

## References

- [capt-hook API reference](references/capt-hook-api.md) — events, primitives, conditions, event object, CLI.
- [Pattern catalog](references/pattern-catalog.md) — one validated hook file per taxonomy category.
- [Testing hooks](references/testing-hooks.md) — inline test format, fixtures, debugging recipes.
- [Pitfalls](references/pitfalls.md) — primitive-choice and dispatch failure modes; read before Step 2.
