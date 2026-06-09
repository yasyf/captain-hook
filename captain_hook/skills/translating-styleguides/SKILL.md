---
name: translating-styleguides
description: Translates a prose style guide (STYLEGUIDE.md, CONTRIBUTING style sections, CLAUDE.md conventions) into enforced captain-hook rules — AST Matcher StyleRules where the rule is mechanical, custom check() logic for complex-but-deterministic rules, LLM gates/nudges for semantic rules — each with inline tests synthesized from the guide's own good/bad examples, plus a final report of what is and is not enforced. Use when the user says "enforce our style guide", "turn STYLEGUIDE.md into hooks", "make Claude follow our coding conventions", or when bootstrapping-hooks finds a style guide.
argument-hint: "[path to styleguide .md] (auto-detects STYLEGUIDE.md / CONTRIBUTING.md / CLAUDE.md when omitted)"
allowed-tools: Read, Grep, Glob, AskUserQuestion, Write, Edit, Bash(uvx capt-hook:*, capt-hook:*, ls:*)
---

# Translating Styleguides

`styleguide()` is a substrate that ships zero rules. A rule is a `StyleRule` subclass:
the docstring is the message (`{violations}` substituted at fire time), `match` is a
`Matcher` composed from `captain_hook.style.matchers` (imported as `M`), the class name
is the identity (`NoNestedImports` → `no-nested-imports`), and inline `tests` make it
verifiable. `StyleDiffRule` flags constructs *introduced by this edit*. Rules are
change-scoped: the whole post-edit file is parsed, but only violations on edited lines
are reported. Each `styleguide(...)` call registers exactly one hook; severity
(warn vs `block=True`) is per call.

This skill turns a prose guide into `.claude/hooks/style.py` (deterministic rules) and
`.claude/hooks/style_llm.py` (semantic rules), then proves them with `uvx capt-hook test`.

## Hard Rules

- Every guide statement lands in the final report — Tier 4 (unenforceable) rows included,
  each with a one-line reason. Never silently drop a rule.
- Every Tier 1-2 rule carries inline `tests` with at least one firing and one allowing
  input. `uvx capt-hook test` must pass before you report.
- Do not write hook files before the user confirms the classification (Step 4).
- Default severity is warn. Propose `block=True` only where the guide says
  "never" / "forbidden", and only if the user opts in.

## Workflow

Copy this checklist into your response and check off steps as you complete them:

```
Translation Progress:
- [ ] Step 1: Locate the guide (argument path or auto-detect)
- [ ] Step 2: Atomize the prose into candidate rules
- [ ] Step 3: Classify each rule by tier (1-4)
- [ ] Step 4: Confirm classification with the user — nothing written before approval
- [ ] Step 5: Write style.py (Tiers 1-2, with tests)
- [ ] Step 6: Write style_llm.py (Tier 3, cost-controlled)
- [ ] Step 7: Verify (uvx capt-hook test, fix until green)
- [ ] Step 8: Enforcement report (every rule, Tier 4 included)
```

### 1. Locate the guide

Use the argument path when given. Otherwise glob, in order: `STYLEGUIDE.md`,
`docs/STYLEGUIDE*.md`, then style/conventions sections of `CONTRIBUTING.md`,
`AGENTS.md`, and `CLAUDE.md`. Follow `@file` includes — an `AGENTS.md` line reading
`@STYLEGUIDE.md` pulls that whole file into scope, so read the target too.

### 2. Atomize the prose

Walk the markdown heading by heading. Each imperative statement ("never X",
"prefer Y over Z", "every A needs B") is one candidate rule. Record per candidate:

- the section heading (it becomes the citation in the rule docstring),
- the verbatim prose stem (it anchors the report row),
- any `# Good` / `# Bad` fenced code examples — these become inline tests.

### 3. Classify each rule by tier

Full criteria and a 12-row worked classification: [references/tier-rubric.md](references/tier-rubric.md).
Condensed:

- **Tier 1 — declarative Matcher.** The violation is a property of one AST node
  (possibly with ancestry/sibling context) expressible by composing the matcher
  vocabulary: `M.kind / M.calls / M.kwarg / M.ref / M.named / M.annotated /
  M.under / M.child_of / M.following`, the prebuilt constants (`M.imports`,
  `M.control_flow`, ...), `& | ~`, and `.where()`. Litmus: can you state it as
  "a node that IS x AND/UNDER y"?
- **Tier 2 — custom `check()`.** Deterministic on the tree but needs cross-node
  aggregation, counting, statement ordering, or body-shape analysis. Matchers still
  serve as selectors inside (`M.func.over(tree)`). Rules about *comments* or
  formatting are not in the AST — use string-mode `lint(fn: (content: str) -> list[str])`.
- **Tier 3 — LLM.** Requires judging intent, scope, naming quality, or "is this
  justified?". `llm_nudge` by default (advisory); `llm_gate` only when the guide says
  "never"; `prompt_check` when the judgment is over an old → new diff.
- **Tier 4 — unenforceable.** Needs context no hook event carries (project-wide
  consistency, review-time aesthetics, PR/process rules), or an LLM hook would fire on
  every edit at prohibitive cost. Goes in the report with a reason.

Non-Python repos: Tiers 1-2 are unavailable (AST rules parse Python only). Degrade to
`hook(Content(r"..."))` regex rules or Tier 3, and say so in the report.

### 4. Confirm with the user

One `AskUserQuestion`. Put the rule → tier table in the question text. Options:

1. **Enforce all as proposed (warn)** — every Tier 1-3 rule, warn severity.
2. **Enforce all, block Tier 1** — Tier 1 rules get their own `styleguide(..., block=True)` call.
3. **Let me adjust** — follow up rule by rule.
4. **Tier 1-2 only (no LLM hooks)** — skip `style_llm.py` entirely.

### 5. Write `.claude/hooks/style.py` (Tiers 1-2)

One `StyleRule` / `StyleDiffRule` per rule. Anatomy:

- **Class name** from the rule statement: `NoMutableDefaults`, `UseComprehensions`.
- **Docstring** = condensed guide prose + `{violations}` + a citation line, e.g.
  `(STYLEGUIDE.md "Error Handling")`. Open with a newline after `"""`; the runner
  normalizes indentation with `inspect.cleandoc`.
- **`match`** = a composed `M.` expression, or **`check()`** override for Tier 2
  (yield `Violation(line, label)`). Full matcher surface and recipes:
  [references/matcher-reference.md](references/matcher-reference.md).
- **`label`** = a short fixed string or `node -> str` callable; omitted, nodes are
  labeled by bound name falling back to `ast.unparse`.
- **`tests`** = inline tests (synthesis procedure below).

Module-level helper functions for `check()` rules live in the same file, above the
classes. Group rules into one `styleguide(...)` call per severity batch:

```python
styleguide(NoBroadExcept, NoMutableDefaults, NoNestedImports, UseComprehensions)
styleguide(NoSqlStrings, block=True, only_if=[FilePath("api/**/*.py")])
```

The built-in guards always apply — `Tool("Edit|Write")`, `FilePath("*.py")`, test files
skipped; `only_if` / `skip_if` narrow from there.

**Test synthesis** when the guide has no code examples:

1. Restate the rule as a predicate over a single construct ("a function parameter
   whose default is a mutable literal").
2. Write the **minimal violating snippet** — at most 5 lines, exactly one construct,
   syntactically complete — as `Input(file="app.py", content=...): Warn()` (or
   `Block()` matching the chosen severity).
3. Write the **minimal compliant twin**, differing only in the one property the rule
   tests, as `Allow()`. Never use a snippet that trips a *different* rule in the same
   `styleguide()` call.
4. For `StyleDiffRule`s, the pair needs `old=`: violating = construct present in
   `content` but absent from `old`; compliant = construct already in `old`
   (pre-existing, must not fire).

When the guide *does* have Good/Bad fences, use them, trimmed to the smallest fragment
that still parses and trips only the rule under test. Add `Warn(pattern=...)` asserting
a distinctive word from the docstring.

Inline-test vocabulary (`from captain_hook import Allow, Block, Input, Warn`):

| Piece | Meaning |
|---|---|
| `Input(file=, content=)` | A Write of `content` to `file` (whole file counts as changed) |
| `Input(file=, old=, content=)` | An Edit replacing `old` with `content` (needed by diff rules) |
| `Warn()` / `Block()` | Hook must warn / block; `pattern=` regex-matches the message |
| `Allow()` | Hook must stay silent |

### 6. Write `.claude/hooks/style_llm.py` (Tier 3)

`llm_nudge` / `llm_gate` / `prompt_check` registrations — full templates and the
cost-control checklist: [references/llm-rule-patterns.md](references/llm-rule-patterns.md).
Always include cost controls: static `only_if` narrowing, `max_fires`, and the
`model="small"` default. `Signal` fields are keyword-only: `Signal(pattern=r"...", weight=2)`.
LLM hooks ship **without** inline `tests` — they would invoke a live model; loading the
file under `capt-hook test` still verifies the registrations import cleanly.

### 7. Verify

Run: `uvx capt-hook test` (add `--json` when parsing). Iterate until green.

**Gotcha:** all rules in one `styleguide()` call merge their tests onto a single hook,
and every `Input` runs through the *whole* styleguide. A failing test usually means the
input trips a sibling rule — shrink it to a single construct that trips exactly one rule.

If `style_llm.py` added hooks on new events (e.g. a `Stop`-targeted `llm_nudge`), run
`uvx capt-hook register-hooks` (it merges non-destructively into `.claude/settings.local.json`
and writes it).

### 8. Enforcement report

Final output — one row per atomized rule, in guide order. When this skill was invoked
by bootstrapping-hooks, append the table to its report:

```
| § / rule (verbatim stem)            | tier | enforcement              | severity | tests |
|-------------------------------------|------|--------------------------|----------|-------|
| Error Handling / "No broad except"  | 1    | style.py::NoBroadExcept  | warn     | 3 ok  |
| Core 6 / "Minimal changes"          | 3    | style_llm.py llm_nudge   | nudge    | —     |
| Core 7 / "Match surrounding code"   | 4    | NOT ENFORCED — needs project-wide judgment per edit |
```

Tier 4 rows are never dropped.

## Worked example: six rules from a real STYLEGUIDE.md

Source guide statements and their classification:

| Guide statement | Tier |
|---|---|
| "No broad `except Exception` that swallows everything" (Error Handling) | 1 |
| "Mutable defaults are forbidden in function signatures" (Functions & Methods) | 1 |
| "Lazy imports ... never inside an `if`, `for`, or `try`" (Type Annotations) | 1 |
| "Use comprehensions instead of imperative accumulation" (Functional Style) | 2 |
| "Minimal changes. Make the test pass, then stop" (Core Principles) | 3 |
| "Match surrounding code. Follow this guide, then the file, then the module" (Core Principles) | 4 |

Generated `.claude/hooks/style.py` (all tests verified with `capt-hook test`):

```python
from __future__ import annotations

import ast
from collections.abc import Iterator

from captain_hook import Allow, Input, Warn
from captain_hook.style import StyleRule, Violation, matchers as M, styleguide

MUTABLE_LITERALS = (ast.List, ast.Dict, ast.Set)


class NoBroadExcept(StyleRule):
    """
    Broad exception handlers swallow every error, including KeyboardInterrupt:
      - {violations}

    Catch a dedicated exception class instead (STYLEGUIDE.md "Error Handling").
    """

    tests = {
        Input(file="app.py", content="try:\n    f()\nexcept:\n    pass\n"): Warn(pattern="Broad"),
        Input(file="app.py", content="try:\n    f()\nexcept Exception:\n    pass\n"): Warn(),
        Input(file="app.py", content="try:\n    f()\nexcept ValueError:\n    pass\n"): Allow(),
    }
    match = M.kind(ast.ExceptHandler).where(
        lambda n: n.type is None or (isinstance(n.type, ast.Name) and n.type.id == "Exception")
    )
    label = "broad except"


class NoMutableDefaults(StyleRule):
    """
    Mutable default arguments are shared across every call:
      - {violations}

    Take `list[T] | None = None` and normalize with `items = items or []`
    (STYLEGUIDE.md "Functions & Methods").
    """

    tests = {
        Input(file="app.py", content="def f(items=[]):\n    pass\n"): Warn(),
        Input(file="app.py", content="def f(items=None):\n    pass\n"): Allow(),
    }
    match = M.func.where(
        lambda n: any(
            isinstance(d, MUTABLE_LITERALS)
            for d in (*n.args.defaults, *(d for d in n.args.kw_defaults if d))
        )
    )
    label = "mutable default"


class NoNestedImports(StyleRule):
    """
    Lazy imports belong at the top of the function body, never inside an `if`,
    `for`, or `try`:
      - {violations}

    (STYLEGUIDE.md "Type Annotations")
    """

    tests = {
        Input(file="app.py", content="def f():\n    if x:\n        import os\n    return 1\n"): Warn(),
        Input(file="app.py", content="def f():\n    import os\n\n    return os.getcwd()\n"): Allow(),
    }
    match = M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)


def empty_list_target(stmt: ast.stmt) -> str | None:
    match stmt:
        case ast.Assign(targets=[ast.Name(id=name)], value=ast.List(elts=[])):
            return name
        case _:
            return None


def only_appends_to(loop: ast.For, name: str) -> bool:
    inner = loop.body[0].body if len(loop.body) == 1 and isinstance(loop.body[0], ast.If) else loop.body
    match inner:
        case [ast.Expr(value=ast.Call(func=ast.Attribute(value=ast.Name(id=target), attr="append")))]:
            return target == name
        case _:
            return False


class UseComprehensions(StyleRule):
    """
    Imperative accumulation that a comprehension expresses in one pass:
      - {violations}

    Build it as `[f(x) for x in xs if cond(x)]` instead (STYLEGUIDE.md "Functional Style").
    """

    tests = {
        Input(
            file="app.py",
            content=(
                "def f(items):\n    out = []\n    for item in items:\n"
                "        if item.ok:\n            out.append(item.value)\n    return out\n"
            ),
        ): Warn(pattern="comprehension"),
        Input(file="app.py", content="def f(items):\n    return [i.value for i in items if i.ok]\n"): Allow(),
    }

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        for node in ast.walk(tree):
            if (body := M.body_of(node)) is None:
                continue
            for prev, loop in zip(body, body[1:]):
                if (name := empty_list_target(prev)) and isinstance(loop, ast.For) and only_appends_to(loop, name):
                    yield Violation(loop.lineno, f"accumulation into `{name}`")


styleguide(NoBroadExcept, NoMutableDefaults, NoNestedImports, UseComprehensions)
```

Why the tiers fell where they did: the first three are single-node predicates
(Tier 1). `UseComprehensions` is a *cross-statement shape* — an `x = []` assignment
followed by a `for` that only appends — inexpressible as one node predicate, so it
overrides `check()` (Tier 2) while reusing `M.body_of` as a body selector.

Generated `.claude/hooks/style_llm.py` — "Minimal changes" is pure intent judgment
(Tier 3), so it becomes a once-per-session nudge at `Stop`:

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

"Match surrounding code" is **Tier 4 — not enforced**: it requires judging local
convention against the whole file and sibling modules on every edit. An LLM hook would
need that full context per `PostToolUse` (prohibitive cost and latency), and the
deterministic sub-cases worth enforcing are already covered by the Tier 1 rules.

Report:

```
| § / rule (verbatim stem)                          | tier | enforcement                  | severity | tests |
|---------------------------------------------------|------|------------------------------|----------|-------|
| Error Handling / "No broad except Exception"      | 1    | style.py::NoBroadExcept      | warn     | 3 ok  |
| Functions & Methods / "Mutable defaults forbidden"| 1    | style.py::NoMutableDefaults  | warn     | 2 ok  |
| Type Annotations / "Lazy imports never inside if" | 1    | style.py::NoNestedImports    | warn     | 2 ok  |
| Functional Style / "Use comprehensions"           | 2    | style.py::UseComprehensions  | warn     | 2 ok  |
| Core / "Minimal changes"                          | 3    | style_llm.py llm_nudge @Stop | nudge    | —     |
| Core / "Match surrounding code"                   | 4    | NOT ENFORCED — needs project-wide judgment per edit |
```

## References

- [references/matcher-reference.md](references/matcher-reference.md) — full `M.` surface, operators, terminals, validated recipes
- [references/tier-rubric.md](references/tier-rubric.md) — full tier criteria + 12-row classification table
- [references/llm-rule-patterns.md](references/llm-rule-patterns.md) — Tier 3 templates + cost-control checklist
