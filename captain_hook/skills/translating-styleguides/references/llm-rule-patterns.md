# LLM Rule Patterns

Three templates for Tier 3 rules, in `.claude/hooks/style_llm.py`. LLM hooks ship
**without** inline `tests` — running them would invoke a live model. `capt-hook test`
still imports the file, which verifies every registration loads.

`Signal` fields are keyword-only: `Signal(pattern=r"...", weight=2)`.

## Contents

- Template 1 — session-scope llm_nudge at Stop
- Template 2 — llm_gate with Signals pre-filter
- Template 3 — prompt_check on a diff
- Cost-control checklist

## Template 1 — session-scope llm_nudge at Stop

For whole-session judgments ("minimal changes", "stay within scope"). Evaluate once,
when the agent stops, instead of on every edit:

```python
from __future__ import annotations

from captain_hook import Event, TouchedFile, llm_nudge

llm_nudge(
    "Compare the agent's edits this session against the user's stated request. "
    "Is the agent changing things beyond the requested scope — refactoring code it wasn't "
    "asked to touch, renaming for taste, adding speculative parameters or files? "
    "Fire only when the drift is unambiguous.",
    message=lambda r: f"Scope drift: {r.reasoning}. STYLEGUIDE.md: make the test pass, then stop.",
    events=Event.Stop,
    only_if=[TouchedFile("**/*.py")],
    max_fires=1,
)
```

- `events=Event.Stop` overrides the `PostToolUse` default; `max_fires=1` overrides the
  nudge default of 3.
- `message` receives a `NudgeVerdict(fire, reasoning)`; surface `r.reasoning` plus the
  guide citation.
- `only_if=[TouchedFile(...)]` skips the LLM entirely on sessions that edited nothing
  relevant.

## Template 2 — llm_gate with Signals pre-filter

For "never X" rules with no deterministic predicate. The signals score recent
transcript text first — free and instant — and the LLM runs only past the threshold:

```python
from captain_hook import Event, Signal, Signals, SourceEdits, TestFile, llm_gate

llm_gate(
    "The style guide forbids defensive coding: no fallbacks, shims, or guards against "
    "impossible states. Does this edit add a fallback path that silently masks a failure "
    "instead of letting it crash? Block only when the fallback is unambiguous.",
    message=lambda r: f"Defensive coding: {r.reasoning}. STYLEGUIDE.md: fail fast, fail loud.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"except Exception", weight=2),
            Signal(pattern=r"fallback|fall back|default to", weight=1),
            Signal(pattern=r"\braise\b", weight=-2),
        ],
        threshold=2,
        window=10,
    ),
    events=Event.PostToolUse,
    only_if=[SourceEdits(lang="py")],
    skip_if=[TestFile()],
    max_fires=2,
)
```

- Negative weights suppress false positives (a nearby `raise` suggests the code is not
  swallowing).
- `llm_gate` defaults to `Stop | SubagentStop` and `max_fires=1`; here it is retargeted
  at edits.
- `message` receives a `GateVerdict(block, reasoning)`.

## Template 3 — prompt_check on a diff

For judgments over an old → new edit ("don't weaken tests", "assert behavior, not
structure"). `prompt_check` runs inside an `@on` handler and returns block/warn/None
from the LLM's `PromptCheckVerdict`:

```python
from captain_hook import BaseHookEvent, Event, HookResult, Prompt, SourceEdits, TestFile, Tool, on, prompt_check

ASSERTION_TEMPLATE = """
You are reviewing a test edit against the style guide rule:
"Write strict assertions against specific expected values; a test that can't fail uncovers nothing."

Warn if the new test only asserts structure (hasattr, isinstance, `is not None`)
or asserts nothing the code under test actually computes.

File: {fp}

--- old ---
{old}
--- new ---
{new}
"""


@on(Event.PostToolUse, only_if=[SourceEdits(lang="py", include_tests=True), TestFile(), Tool("Edit")])
def review_test_assertions(evt: BaseHookEvent) -> HookResult | None:
    if not (fp := evt.file) or not (old := evt.old) or not (new := evt.content):
        return None
    return prompt_check(
        evt,
        Prompt.from_template(ASSERTION_TEMPLATE, fp=fp.path, old=old, new=new),
        prefix="TEST QUALITY",
        suffix=" If unsure, allow.",
    )
```

- The early return keeps the LLM out of every edit that lacks a real old → new pair.
- `prefix` labels the verdict reason; `suffix` carries the standing instruction. Bias
  toward allowing — an over-eager style judge erodes trust fast.

## Cost-control checklist

Apply to every Tier 3 hook:

- [ ] `signals=` pre-filter, or a static `only_if` narrow enough that most events never
      reach the LLM
- [ ] `max_fires` set deliberately (gates default to 1, nudges to 3)
- [ ] `max_context` left at the 2000-char default unless the judgment needs more
- [ ] `model="small"` (the default) unless accuracy demands bigger
- [ ] `skip_if=[TestFile()]` or equivalent exclusions for files the rule exempts
- [ ] For stateless yes/no checks, pass `agent=False, transcript=False`

The framework enforces **one LLM fire per turn** across all LLM primitives, so stacked
Tier 3 hooks cannot multiply cost within a single dispatch cycle. Latency still
matters: an LLM evaluation adds seconds, so prefer `Stop`-targeted hooks over
`PreToolUse` ones, which delay the blocked tool call.
