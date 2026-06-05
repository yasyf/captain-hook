# Style Rules

`styleguide()` turns AST-based style checks into a hook. It is a *substrate*: captain-hook
ships **no rules of its own** — you author them as [`StyleRule`][captain_hook.StyleRule]
subclasses and register them. The framework owns the plumbing — parsing, change-scoping,
message formatting, and test wiring — so a rule is just a short `check` method.

## Your first rule

A rule is a subclass. Write the message as the class **docstring** (`{violations}` is
substituted at fire time), implement `check`, and hand the class to `styleguide()`:

```python
import ast
from captain_hook import styleguide, StyleRule, Violation, Input, Warn, Allow

class NoPrint(StyleRule):
    """
    print() calls don't belong in committed code:
      - {violations}

    Use a logger (logger.info(...)) instead.
    """

    trigger = "print"                       # fast-exit if "print" isn't in the source
    tests = {
        Input(file="app.py", content="def f():\n    print('hi')\n"): Warn(),
        Input(file="app.py", content="def f():\n    logger.info('hi')\n"): Allow(),
    }

    def check(self, tree: ast.Module):
        for node in ast.walk(tree):
            match node:
                case ast.Call(func=ast.Name(id="print")):
                    yield Violation(node.lineno, "print() call")

styleguide(NoPrint)
```

- The **class name is the identity** — `NoPrint` becomes `no-print` (kebab-case).
- The **docstring is the message**. Open it with a newline after `"""`; the runner normalizes
  it with `inspect.cleandoc`, so your indentation never leaks into the output. `{violations}`
  is replaced with the rule's findings joined by `sep` (default a bulleted list).
- `check` walks the post-edit AST and yields [`Violation(line, label)`][captain_hook.Violation].
  The runner renders each as `label (line N)`.
- `trigger` is an optional substring fast-exit: if it isn't present in the source, the rule is
  skipped without parsing the AST.

## Change scoping

A rule sees the **whole post-edit file** (so a check never fails to parse a partial edit
fragment) but reports **only violations on the lines your edit changed**. Editing one function
does not surface a pre-existing `print()` in another function of the same file:

```python
# file already contains print() in two functions; you edit only the second.
# -> only the print() on the line you touched is reported.
```

A `Write` (whole new file) counts as fully changed, so every violation is reported. This is the
"see the changes, but parse enough context not to error" contract — you get full-module context
for correctness and edit-scoped reporting for signal.

## Diff rules

When a rule must compare *before and after* — "did this edit **introduce** something?" —
subclass [`StyleDiffRule`][captain_hook.StyleDiffRule]. Its `check` receives both trees:

```python
from captain_hook import StyleDiffRule, Violation

class NoNewWildcardImport(StyleDiffRule):
    """
    Wildcard import added by this edit:
      - {violations}
    """

    def check(self, pre: ast.Module, post: ast.Module):
        old = {n.module for n in ast.walk(pre)
               if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)}
        for node in ast.walk(post):
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names) \
               and node.module not in old:
                yield Violation(node.lineno, f"from {node.module} import *")
```

The pre-edit tree is reconstructed from the edit, so the rule fires only on a *newly added*
wildcard, not one that was already there.

## Scope and severity — one hook per call

Each `styleguide(...)` call registers **exactly one hook**, scoped by that call. Every axis —
including block-vs-warn — is per call, so split concerns into separate calls:

```python
styleguide(NoPrint, NoBareExcept)                                  # warn, all *.py
styleguide(NoSqlInjection, block=True, only_if=[FilePath("api/**/*.py")])  # block, api only
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

## The query API

Rather than hand-walking the tree, compose a `Query`. It is a fluent, immutable filter over the
AST: start with `Query.of(tree)`, chain refinements, and finish with `violations(label)` (or
iterate it / call `exists()`):

```python
from captain_hook.styleguide import Query, Import, ControlFlow, TypeChecking

def check(self, tree):
    yield from (
        Query.of(tree)
        .matching(Import)                 # Import / ImportFrom nodes
        .directly_inside(ControlFlow)     # whose immediate parent is an if/for/try/with/...
        .not_inside(TypeChecking)         # but not under `if TYPE_CHECKING:`
        .violations(ast.unparse)          # -> Violation(line, ast.unparse(node))
    )
```

Refinements: `matching(kind)`, `where(predicate)`, `inside(kind)`, `directly_inside(kind)`,
`not_inside(kind)`, and `after_first(kind)` (keep body-statements following the first sibling of
a kind — the anchor for "declarations before code" rules).

### Kinds

A `Kind` is a matchable category of node — a set of node types and/or a predicate. The built-in
kinds cover the common cases and compose with `|`:

```python
from captain_hook.styleguide import (
    Kind, Module, Class, Function, Definition, Import, Call, Assignment, ControlFlow, TypeChecking,
)

Definition            # Class | Function
Decorated = Kind(test=lambda n: bool(getattr(n, "decorator_list", None)))   # roll your own
```

Factories build parameterized kinds: `calls("zip")` matches calls to a named function, `named("x", "y")`
matches a class/function/assignment/argument bound to one of those names. Because you compose
kinds and drop into `where(...)` for anything bespoke, new rules rarely need a new framework
helper.

### Annotation & node helpers

For checks that inspect annotations or names, `captain_hook.styleguide` also exports
`name_of(node)`, `is_name(expr, "Any")`, `has_keyword(call, "strict")`, `has_future_annotations(tree)`,
`annotations(tree)`, `string_literals(expr)`, and `annotated_slots(tree)` (each annotated
variable/return/param as a `Slot`). For example, a "no widening to `Any`" diff rule is just:

```python
from captain_hook.styleguide import annotated_slots, is_name

def check(self, pre, post):
    before = {s.name for s in annotated_slots(pre) if is_name(s.annotation, "Any")}
    yield from (
        Violation(s.line, f"{s.name}: Any")
        for s in annotated_slots(post)
        if is_name(s.annotation, "Any") and s.name not in before
    )
```

## Testing rules

Attach inline `tests` to each rule and run them with `capt-hook test`:

```python
tests = {
    Input(file="app.py", content="print('x')\n"): Warn(),
    Input(file="app.py", content="x = 1\n"): Allow(),
}
```

Tests for every rule in a call are merged onto its hook and each `Input` runs through the whole
styleguide, so keep inputs minimal — a single construct that trips exactly one rule. Use
`Warn(pattern=...)` or `Block(pattern=...)` to assert against the message text.
