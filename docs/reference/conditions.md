# Conditions Cheatsheet

Quick reference for all condition types. Use in `only_if` (all must match) or `skip_if` (any skips the hook).

## Current Event

These match properties of the event being processed right now.

| Condition | Matches | Pattern type | Example |
|-----------|---------|-------------|---------|
| `Tool(p)` | Tool name | Regex | `Tool("Bash\|Execute")` |
| `FilePath(p)` | File path in event | Glob | `FilePath("**/*.py")` |
| `Command(p)` | Bash command text | Regex | `Command(r"git\s+stash")` |
| `Content(p)` | File content (Write/Edit) | Regex | `Content(r"print\(")` |
| `Agent(p)` | Subagent type | Regex | `Agent("cleanup-.*")` |
| `TestFile()` | File is a test file | -- | `TestFile()` |

`FilePath`, `Content`, and `TestFile` match project files only by default. Absolute paths outside the hook
root are ignored. Use `project_only=False` when a hook intentionally targets external scratch files,
attachments, or logs.

## Transcript History

These check what happened earlier in the session.

| Condition | Matches | Pattern type | Example |
|-----------|---------|-------------|---------|
| `ReadFile(p)` | A file was read | Glob | `ReadFile("TESTING.md")` |
| `TouchedFile(p)` | A file was edited | Glob | `TouchedFile("**/*.py")` |
| `RanCommand(p)` | A command was run | Regex | `RanCommand(r"uv\s+run\s+mtest")` |
| `UsedSkill(p)` | A skill was invoked | Exact (`\|`-alternation) | `UsedSkill("codex\|codex:codex")` |
| `InPlanMode()` | Agent is in plan mode | -- | `InPlanMode()` |

## Logic

```python
only_if=[A, B, C]   # A AND B AND C must all match
skip_if=[X, Y]      # X OR Y — any match skips the hook
```

Both lists can be used together. `skip_if` is checked first.

## Naming Convention

| Scope | Pattern | Examples |
|-------|---------|---------|
| Current event | Bare noun | `Tool`, `FilePath`, `Command`, `Content`, `TestFile` |
| Transcript history | Past-tense / compound | `ReadFile`, `TouchedFile`, `RanCommand`, `UsedSkill`, `InPlanMode` |

## Custom Conditions

Implement the `CustomCondition` protocol:

```python
from dataclasses import dataclass
from captain_hook import CustomCondition
from captain_hook.events import BaseHookEvent

@dataclass
class MinEdits(CustomCondition):
    threshold: int = 5

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.ctx.t.tool_uses.where(name="Edit").count() >= self.threshold
```

Use like any built-in condition:

```python
hook(Event.Stop, message="Too many edits", block=True, only_if=[MinEdits(threshold=10)])
```
