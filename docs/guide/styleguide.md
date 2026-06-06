# Style Rules

`styleguide()` turns AST-based style checks into a hook. It is a *substrate*: captain-hook
ships **no rules of its own** — you author them as [`StyleRule`][captain_hook.StyleRule]
subclasses and register them. The framework owns the plumbing — parsing, change-scoping,
message formatting, and test wiring — so a rule is usually just **data**: a docstring and a
[`Matcher`][captain_hook.styleguide.Matcher].

## Your first rule

A rule is a subclass. Write the message as the class **docstring** (`{violations}` is
substituted at fire time), set `match` to a [`Matcher`][captain_hook.styleguide.Matcher], and
hand the class to `styleguide()`:

```python
from captain_hook import styleguide, StyleRule, Input, Warn, Allow
from captain_hook.styleguide import Matcher

class ZipStrict(StyleRule):
    """
    zip() without strict=True can silently drop items:
      - {violations}

    Pass strict=True so length mismatches raise.
    """

    trigger = "zip"                         # fast-exit if "zip" isn't in the source
    tests = {
        Input(file="app.py", content="zip(a, b)\n"): Warn(),
        Input(file="app.py", content="zip(a, b, strict=True)\n"): Allow(),
    }
    match = Matcher.calls("zip") & ~Matcher.kwarg("strict")
    label = "zip()"

styleguide(ZipStrict)
```

- The **class name is the identity** — `ZipStrict` becomes `zip-strict` (kebab-case).
- The **docstring is the message**. Open it with a newline after `"""`; the runner normalizes
  it with `inspect.cleandoc`, so your indentation never leaks into the output. `{violations}`
  is replaced with the rule's findings joined by `sep` (default a bulleted list).
- `match` selects the offending nodes; each becomes a [`Violation`][captain_hook.Violation]
  rendered as `label (line N)`. `label` may be a fixed string (as here), a `node -> str`
  callable, or omitted — the default labels a node by its bound name, falling back to
  `ast.unparse`.
- `trigger` is an optional substring fast-exit: if it isn't present in the source, the rule is
  skipped without parsing the AST.

## Change scoping

A rule sees the **whole post-edit file** (so a check never fails to parse a partial edit
fragment) but reports **only violations on the lines your edit changed**. Editing one function
does not surface a pre-existing `zip()` in another function of the same file. A `Write` (whole
new file) counts as fully changed, so every violation is reported. You get full-module context
for correctness and edit-scoped reporting for signal.

## Diff rules

When a rule must compare *before and after* — "did this edit **introduce** something?" —
subclass [`StyleDiffRule`][captain_hook.StyleDiffRule]. It flags nodes matching `match` in the
new tree whose identity (`key`, default `ast.unparse`) was absent from the old tree:

```python
from captain_hook import StyleDiffRule
from captain_hook.styleguide import Matcher

class NoNewWildcardImport(StyleDiffRule):
    """
    Wildcard import added by this edit:
      - {violations}
    """

    match = Matcher.imports.where(lambda n: any(a.name == "*" for a in n.names))
```

The pre-edit tree is reconstructed from the edit, so the rule fires only on a *newly added*
wildcard, not one already there. Set `key` to a custom `node -> Hashable` when `ast.unparse`
isn't the right notion of "the same construct" (e.g. comparing annotated slots by name).

## The Matcher

A [`Matcher`][captain_hook.styleguide.Matcher] is one composable thing: a node predicate that
is also a tree selector. Build rules by **combining matchers**, not by reaching for a framework
helper. Compose with a boolean algebra and refine with `.where(...)`:

```python
from captain_hook.styleguide import Matcher

Matcher.imports & Matcher.child_of(Matcher.control_flow) & ~Matcher.under(Matcher.type_checking)
Matcher.cls & Matcher.private                         # private-named class
Matcher.kind(ast.Lambda)                              # a node type with no shipped name
Matcher.call.where(lambda n: len(n.args) > 5)         # bespoke one-off predicate
```

| Group | Members |
|-------|---------|
| Operators | `&` (both), `\|` (either), `~` (negate) |
| Node categories | `Matcher.module`, `.cls`, `.func`, `.definition` (`cls \| func`), `.imports`, `.call`, `.assignment`, `.control_flow`, `.type_checking`, `.any` |
| Predicates | `Matcher.calls(name)`, `.kwarg(name)`, `.ref(name)`, `.named(pattern)`, `.kind(*types)` |
| Name conventions | `Matcher.private` (leading single underscore), `.dunder`, `.constant` (`SCREAMING_SNAKE`) |
| Annotations | `Matcher.annotated(inner=None)` (annotated var/param/return), `.forward_ref` (quoted type ref), `.future_annotations` (a module with `from __future__ import annotations`) |
| Structure | `Matcher.under(m)` (any ancestor), `.child_of(m)` (immediate parent), `.following(m)` (sibling after the first match) |
| Terminals | `.over(tree)`, `.violations(tree, label=None)`, `.exists(tree)`, `.matches(node)`, `.diff(pre, post, key, label=None)` |

`~Matcher.under(x)` is "not inside `x`" — a single negation operator, not a separate method.
The name-convention matchers mean rule files never re-declare `UPPER_SNAKE` / underscore
regexes; reach for `Matcher.named(r"...")` only for a one-off pattern. Annotations compose like
everything else — a "no quoted annotations under PEP 563" rule is just
`Matcher.forward_ref & Matcher.under(Matcher.future_annotations)`.

## Custom logic with check()

When a rule's logic genuinely can't be expressed as a matcher — cross-node aggregation, body
normalization, anything stateful — override `check()` instead of setting `match`. It receives
the post-edit `ast.Module` (or both trees for a `StyleDiffRule`) and yields `Violation`s. A
matcher is still useful inside it as a selector via `.over(tree)`:

```python
class NoStructuralOnlyTests(StyleRule):
    """Tests that only assert with builtins exercise nothing: {violations}"""

    def check(self, tree):
        for fn in Matcher.func.over(tree):
            if fn.name.startswith("test_") and only_builtin_calls(fn):
                yield Violation(fn.lineno, fn.name)
```

## Scope and severity — one hook per call

Each `styleguide(...)` call registers **exactly one hook**, scoped by that call. Every axis —
including block-vs-warn — is per call, so split concerns into separate calls:

```python
styleguide(NoPrint, NoBareExcept)                                         # warn, all *.py
styleguide(NoSqlInjection, block=True, only_if=[FilePath("api/**/*.py")]) # block, api only
```

The built-in `Tool("Edit|Write")` and `FilePath("*.py")` guards always apply (and test files
are skipped); `only_if` / `skip_if` **narrow** from there. In `block=True`, the single hook
returns one block listing every violation at once.

| Parameter | Description |
|-----------|-------------|
| `*rules` | `StyleRule` / `StyleDiffRule` subclasses to apply |
| `block` | Block the tool call instead of warning |
| `only_if` / `skip_if` | Extra conditions, ANDed/ORed onto the built-in guards |
| `events` | Override the default `PostToolUse` targeting |
| `max_shown` | Maximum violations shown per rule (default 5) |

## Testing rules

Attach inline `tests` to each rule and run them with `capt-hook test`:

```python
tests = {
    Input(file="app.py", content="zip(a, b)\n"): Warn(),
    Input(file="app.py", content="zip(a, b, strict=True)\n"): Allow(),
}
```

Tests for every rule in a call are merged onto its hook and each `Input` runs through the whole
styleguide, so keep inputs minimal — a single construct that trips exactly one rule. Use
`Warn(pattern=...)` or `Block(pattern=...)` to assert against the message text.
