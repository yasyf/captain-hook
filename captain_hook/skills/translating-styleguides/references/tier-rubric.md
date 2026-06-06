# Tier Rubric

Classify every atomized guide statement into exactly one tier. When a statement spans
tiers ("prefer comprehensions; never shadow builtins"), split it into one candidate per
enforceable predicate.

## Tier 1 — declarative Matcher

The violation is a property of **one AST node**, possibly with ancestry or sibling
context. Expressible by composing the matcher vocabulary — `M.kind / M.calls /
M.kwarg / M.ref / M.named / M.annotated / M.under / M.child_of / M.following`, the
prebuilt constants, `& | ~`, and `.where()` for the last mile.

Litmus: can you state the rule as *"a node that IS x AND/UNDER y"*? Then it is Tier 1,
even when the predicate needs a `.where()` lambda.

```python
class ZipStrict(StyleRule):
    """zip() without strict=True can silently drop items: {violations}"""

    match = M.calls("zip") & ~M.kwarg("strict")
```

Use `StyleDiffRule` instead of `StyleRule` when the guide says "don't *add*" / "no
*new*" — the rule then fires only on constructs absent from the pre-edit tree.

## Tier 2 — custom check()

Deterministic on the tree, but the logic needs **cross-node aggregation**: counting,
statement ordering, body-shape analysis, or comparing two constructs. Override
`check(self, tree)` and yield `Violation(line, label)`; matchers still serve as
selectors inside (`M.func.over(tree)`, `M.body_of(node)`).

Special case: rules about **comments, docstrings, blank lines, or formatting** are not
in the AST at all. Use the string-mode `lint()` primitive instead — pass a
`(content: str) -> list[str]` function and it runs against the raw file text.

## Tier 3 — LLM

The rule requires judging **intent, scope, naming quality, or justification** — no
deterministic predicate exists. Pick the primitive by shape:

- `llm_nudge` — the default; advisory, fires as a warning.
- `llm_gate` — only when the guide says "never" / "forbidden"; blocks.
- `prompt_check` — when the judgment is over an **old → new diff** (inside an `@on`
  handler with `evt.old` / `evt.content`).

Templates and cost controls: [llm-rule-patterns.md](llm-rule-patterns.md).

## Tier 4 — unenforceable

No hook event carries the needed context, or an LLM hook would have to fire on every
edit at prohibitive cost or latency. Typical shapes:

- project-wide consistency ("match surrounding code", "follow module conventions"),
- review-time aesthetics ("names should read well"),
- PR/process rules ("atomic commits", "one logical change per commit").

Tier 4 rules go in the enforcement report with a one-line reason. **Never silently
dropped** — the user must see what their guide asks for that hooks cannot deliver.

## Example classifications

| Guide statement | Tier | Enforcement |
|---|---|---|
| "no print() in committed code" | 1 | `M.calls("print")` |
| "zip() needs strict=True" | 1 | `M.calls("zip") & ~M.kwarg("strict")` |
| "no wildcard imports added" | 1 | `StyleDiffRule` + `M.imports.where(lambda n: any(a.name == "*" for a in n.names))` |
| "class-body assignments before any methods" | 1 | `M.assignment & M.child_of(M.cls) & M.following(M.func)` |
| "every module needs `from __future__ import annotations`" | 1 | `M.module & ~M.future_annotations` |
| "never widen a typed slot to Any" | 1 | `M.annotated(M.ref("Any"))` |
| "use comprehensions over imperative accumulation" | 2 | `check()` pairing an `x = []` with an append-only `for` |
| "keep try blocks minimal — only the throwing line inside" | 2 | `check()` counting `ast.Try` body statements |
| "no comments except TODOs" | 2 | string-mode `lint()` — comments are not in the AST |
| "tests must assert behavior, not structure" | 3 | `prompt_check` on test-file edits |
| "minimal changes — stay within scope" | 3 | `llm_nudge` at `Stop`, `max_fires=1` |
| "match surrounding code" | 4 | NOT ENFORCED — needs whole-project judgment per edit |

Process rules ("atomic commits") are Tier 4 by default; gate them only when the guide
is explicit about a command ritual — that is command-hook territory
(`gate` / `block_command` on `Command(r"git\s+commit")`), not a style rule.

## Non-Python repos

`StyleRule` / `StyleDiffRule` / AST-mode `lint()` parse Python only. For other
languages, degrade:

- Tier 1 candidates → `hook(Event.PostToolUse, only_if=[FilePath("*.ts"), Content(r"...")], message=...)`
  regex rules — coarser, so prefer warn severity.
- Tier 2 candidates → string-mode `lint()` when line-oriented, else Tier 3.
- Note the degradation in the report's enforcement column ("regex, not AST").
