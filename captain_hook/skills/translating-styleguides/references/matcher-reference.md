# Matcher Reference

The full surface of `captain_hook.style.matchers`. Canonical import:

```python
from captain_hook.style import StyleDiffRule, StyleRule, matchers as M, styleguide
```

A `Matcher` is one composable thing: a node predicate that is also a tree selector.
*Node-local* matchers (`M.calls`, `M.kind`, ...) test a node in isolation; *structural*
matchers (`M.under`, `M.child_of`, `M.following`, `M.forward_ref`, `M.annotated`)
consult the parent map. Build rules by combining matchers, not by writing traversal code.

## Contents

- Operators
- Prebuilt constants
- Predicate factories
- Structure
- Refinement: `.where()`
- Terminals
- Helpers for check()
- Recipes

## Operators

| Operator | Meaning |
|---|---|
| `a & b` | both match |
| `a \| b` | either matches |
| `~a` | does not match — `~M.under(x)` is "not inside x" |

## Prebuilt constants

| Constant | Matches |
|---|---|
| `M.module` | `ast.Module` (the file root) |
| `M.cls` | a class definition |
| `M.func` | a sync or async function definition |
| `M.definition` | `M.cls \| M.func` |
| `M.imports` | `import x` / `from x import y` |
| `M.call` | any call expression |
| `M.assignment` | `x = ...` / `x: T = ...` |
| `M.control_flow` | `if` / `for` / `while` / `with` / `try` / `except` (incl. async) |
| `M.type_checking` | an `if TYPE_CHECKING:` block |
| `M.future_annotations` | a module containing `from __future__ import annotations` |
| `M.forward_ref` | a quoted (string) type reference inside an annotation |
| `M.private` | a definition/assignment/parameter named `_x` (single leading underscore) |
| `M.dunder` | one named `__x__` |
| `M.constant` | one named `UPPER_SNAKE` (optional leading underscore) |

## Predicate factories

| Factory | Matches |
|---|---|
| `M.kind(*types, label=None)` | any of the given `ast` node types — the primitive for a category not shipped, e.g. `M.kind(ast.Lambda)` |
| `M.calls(name)` | a call to the bare-name function `name`, e.g. `M.calls("zip")` |
| `M.kwarg(name)` | a call passing keyword argument `name` — combine with `M.calls` |
| `M.ref(name)` | a bare name reference, e.g. `M.ref("Any")` |
| `M.named(pattern)` | a class/function/assignment/parameter whose bound name matches the regex (`re.search`) |
| `M.annotated(inner=None)` | an annotation owner (annotated variable, parameter, or return; excludes `*args`/`**kwargs`); with `inner`, its annotation expression must also match, e.g. `M.annotated(M.ref("Any"))` |

## Structure

| Factory | Matches |
|---|---|
| `M.under(m)` | a node with *any ancestor* matching `m` |
| `M.child_of(m)` | a node whose *immediate parent* matches `m` |
| `M.following(m)` | a body statement that comes *after the first sibling* matching `m` |

## Refinement: `.where()`

The escape hatch for bespoke node-local conditions — keeps the rule declarative while
covering the last mile:

```python
M.call.where(lambda n: len(n.args) > 5)
M.imports.where(lambda n: any(a.name == "*" for a in n.names))
```

## Terminals

| Terminal | Returns |
|---|---|
| `.over(tree)` | iterator of every matching node in `tree` |
| `.violations(tree, label=None)` | a `Violation(line, label)` per match — what a declarative `StyleRule` runs |
| `.exists(tree)` | whether any node matches |
| `.matches(node)` | test a single node — **raises `ValueError` for structural matchers**, which need tree context; use `.over()` / `.violations()` instead |
| `.diff(pre, post, key=ast.unparse, label=None)` | violations for matches in `post` whose `key` was absent from `pre` — what a `StyleDiffRule` runs; override `key` when `ast.unparse` isn't the right identity |

`label` may be a fixed string or a `node -> str` callable; omitted, nodes are labeled
by bound name, falling back to `ast.unparse`.

## Helpers for check()

Module-level functions in `M`, useful inside Tier 2 `check()` overrides:
`M.body_of(node)` (the statement list of a module/def/loop/branch, or `None`),
`M.name_of(node)` (the bound name, or `None`), `M.parent_map(tree)`.

## Recipes

All validated against real trees:

```python
M.calls("zip") & ~M.kwarg("strict")                          # zip() without strict=
M.calls("print")                                             # print() call
M.imports & M.child_of(M.control_flow) & ~M.under(M.type_checking)  # import nested in if/for/try
M.imports.where(lambda n: any(a.name == "*" for a in n.names))      # wildcard import (pair with StyleDiffRule)
M.module & ~M.future_annotations                             # module missing `from __future__ import annotations`
M.forward_ref & M.under(M.future_annotations)                # quoted annotation despite PEP 563
M.cls & M.private                                            # private-named class
M.assignment & M.child_of(M.cls) & M.following(M.func)       # class-body assignment after the first method
M.annotated(M.ref("Any"))                                    # a slot annotated Any
M.kind(ast.Global)                                           # global statement
M.call.where(lambda n: len(n.args) > 5)                      # call with more than 5 positional args
M.func & M.dunder                                            # dunder method definition
```

Used as a `StyleDiffRule.match`, a recipe flags only *newly introduced* occurrences:

```python
class NoNewWildcardImport(StyleDiffRule):
    """Wildcard import added by this edit: {violations}"""

    match = M.imports.where(lambda n: any(a.name == "*" for a in n.names))
```
