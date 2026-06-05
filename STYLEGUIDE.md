# captain-hook Style Guide

The concrete style rules for `captain_hook/`. Target Python 3.12+.

## Core Principles

1. **Functional over imperative.** Compose, chain, and return. Skip intermediate
   variables when a pipeline reads well, and reach for the walrus (`:=`) and
   comprehensions instead of loops.
2. **Match for dispatch.** Pattern matching for type dispatch, destructuring, and
   multi-factor decisions. Use `if/elif` only for meaningful boolean flags.
3. **Type everything.** `from __future__ import annotations` in every module.
   Never widen a typed slot to `Any` to quiet the checker.
4. **Fail fast, fail loud.** No fallbacks, shims, or backwards-compat layers. No
   sentinel values, no silent defaults. Crash on the unexpected.
5. **Make invalid states unrepresentable.** `NewType` for branded primitives,
   frozen dataclasses for immutable data, required fields over optionals.
6. **Minimal changes.** Stay within scope. Make the test pass, then stop. Improve
   only the code you touch.
7. **Match surrounding code.** Follow this guide first, then the file you're in,
   then the module. If surrounding code violates this guide, fix it.

## Functional Style

Avoid intermediate variables. Chain operations or return directly.

```python
# Good
def expand_tool_names(name: str) -> set[str]:
    return (base := set(name.split("|"))) | {
        alias for n in base for alias in (TOOL_ALIASES.get(n), TOOL_ALIASES_REVERSE.get(n)) if alias
    }

# Bad
def expand_tool_names(name):
    base = set(name.split("|"))
    aliases = set()
    for n in base:
        ...
    return base | aliases
```

Use the walrus operator to bind a value once and reuse it inside an expression.

```python
# Good
if (match := WHEEL_CHECKSUM.search(body)):
    return match.group(1)

# Good — walrus in a comprehension, single pass
return [result for item in items if (result := process(item)) is not None]
```

Prefer the dict union operator over unpacking.

```python
config = defaults | user_config | overrides   # not {**defaults, **user_config, ...}
```

Use comprehensions instead of imperative accumulation.

```python
# Good
return [item.transform() for item in items if item.is_valid()]

# Bad
result = []
for item in items:
    if item.is_valid():
        result.append(item.transform())
return result
```

## Type Annotations

Always annotate. Use future annotations and guard expensive or cycle-prone imports
with `TYPE_CHECKING`. Under PEP 563 annotations stay strings, so they need no quotes.

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

def check(self, evt: BaseHookEvent) -> bool: ...
```

Lazy imports that break cycles or defer heavy modules go at the top of the function
body, before any logic, and never inside an `if`, `for`, or `try`.

```python
# Good
def model_version() -> str:
    from captain_hook.state import RESOURCES

    return RESOURCES.lookup()

# Bad — import buried in a branch
def model_version() -> str:
    if cached:
        from captain_hook.state import RESOURCES
        ...
```

Don't widen to `Any` to quiet pyright. Use the real type, narrow with `isinstance`,
or split the model. Trivial complaints such as `cached_property` shadowing
`property` or descriptor-protocol nuances are noise; ignore them instead of reaching
for `# type: ignore`. Wanting `hasattr` on a typed object means the type is wrong.
Fix it or define a `Protocol`.

## Pattern Matching

Use `match` for type dispatch, destructuring, and decisions that turn on several
factors at once.

```python
match decision:
    case Keep():
        return msg
    case Compress(rate=rate):
        return msg.filter(lambda c: c.type != "text").append(compress(text, rate))
    case Summarize(content=content):
        return msg.append(content)
```

For multi-factor decisions, name the state with a `NamedTuple` so each `case` maps
one-to-one onto a requirement.

```python
match Status(is_fresh, scores.get(id(tc))):
    case Status(score=None):           return tc
    case Status(score=s) if s >= floor: return tc
    case Status(is_fresh=True):        return tc.demote()
    case Status(is_fresh=False):       return tc.exclude()
```

Use `if/elif` when the branches turn on meaningful boolean flags with their own
names. Don't build a tuple just to pattern-match on it.

## Error Handling

Keep `try` blocks minimal. Only the line that can throw belongs inside.

```python
# Good
try:
    response = await client.fetch(url)
except HTTPError:
    return None
data = response.json()
return transform(data)
```

No broad `except Exception` that swallows everything. Use dedicated exception
classes. Read required configuration with `os.environ["KEY"]` so a missing key
raises at startup. No sentinel return values; raise, or return a typed result.

## Code Organization

Module order runs imports, constants, type aliases, helpers, classes, then
functions. Module-level `UPPER_SNAKE_CASE` constants sit immediately after imports,
before any class or function.

Within a class body, all assignments come before any methods. That covers
constants, `ClassVar`s, and dataclass fields.

```python
@dataclass(frozen=True, slots=True)
class HookSpec:
    events: Event
    only_if: tuple[TCondition, ...] = ()
    block: bool = False

    def matches(self, evt: BaseHookEvent) -> bool: ...
```

No leading underscores on classes, constants, or module-level helpers. Use
`__all__` for export control. Reserve a leading underscore for a private instance
attribute.

Frozen dataclasses for immutable and config data. Every mutable default needs a
factory such as `field(default_factory=list)`; a bare `[]` or `{}` is a bug.

## Comments & Docstrings

Code documents itself through names, types, and organization. No comments except
TODOs, non-obvious workarounds, or disabled code.

Docstrings are the one exception, scoped by surface. Public API surfaces such as
`captain_hook/types.py`, the primitives, and user-facing classes carry Google-style
docstrings; mkdocstrings renders them into the docs site, so they earn their place.
Internal helpers get none, and a docstring that restates the signature is clutter to
delete.

```python
# Good — public condition, documented; example renders on the docs site
@dataclass(frozen=True, slots=True)
class Tool:
    """Condition matching the current event's tool name against a regex pattern.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Tool("Bash")], message="...", block=True)
    """

    pattern: str

# Good — internal helper, no docstring
def version_key(dirname: str) -> tuple[int, ...]:
    return tuple(int(part) for part in dirname.removeprefix(f"{MODEL_NAME}-").split("."))
```

## Testing

Tests live in `tests/`; run them with `uv run pytest`. Hook authors also write
inline `tests = {...}` on each hook, runnable with `captain-hook test`.

Write strict assertions against specific expected values; a test that can't fail
uncovers nothing. Mock the boundaries your code talks to, such as the network,
filesystem, and clock, and leave the function under test real. Parameterize repeated
test bodies, giving each case a descriptive `id` and its own expected values.
