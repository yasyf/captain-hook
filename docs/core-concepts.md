# Core Concepts

## Events

Hooks are triggered by **events** — lifecycle moments in Claude Code's execution.
Each event type is a member of the `Event` flag enum:

| Event | When it fires | Typical use |
|-------|--------------|-------------|
| `PreToolUse` | Before a tool runs | Block dangerous commands, enforce policies |
| `PostToolUse` | After a tool succeeds | Lint output, nudge about conventions |
| `PostToolUseFailure` | After a tool fails | Suggest debugging steps |
| `UserPromptSubmit` | User sends a message | Detect multi-request patterns |
| `Stop` | Agent is about to stop | Gate on test execution, review quality |
| `SubagentStop` | A subagent finishes | Verify subagent work quality |
| `SubagentStart` | A subagent launches | Capture initial state |
| `Notification` | Informational event | Logging, metrics |
| `PreCompact` | Before context compaction | Preserve critical context |

Events can be combined with `|`:

```python
hook(Event.Stop | Event.SubagentStop, message="Run tests first", block=True)
```

### Event classes

Each `Event` flag maps to a strongly-typed event class:

- `Event.PreToolUse` → `PreToolUseEvent` — has `tool_name`, `tool_input`
- `Event.PostToolUse` → `PostToolUseEvent` — has `tool_name`, `tool_input`
- `Event.PostToolUseFailure` → `PostToolUseFailureEvent` — has `tool_name`, `tool_input`, `error`
- `Event.Stop` → `StopEvent`
- `Event.SubagentStop` → `SubagentStopEvent` — has `agent_type`
- `Event.SubagentStart` → `SubagentStartEvent` — has `agent_type`
- `Event.UserPromptSubmit` → `UserPromptSubmitEvent` — has `prompt`

All events provide a `ctx` property returning a `HookContext` with access to transcript, settings, session state, CLI helpers, and LLM invocation.

## Conditions

Conditions filter *when* a hook fires. They come in two lists:

- **`only_if`** — all conditions must match (AND logic)
- **`skip_if`** — any matching condition skips the hook (OR logic)

### Current-event conditions

These match properties of the event being processed:

| Condition | Matches | Example |
|-----------|---------|---------|
| `Tool(pattern)` | Tool name (regex) | `Tool("Bash\|Execute")` |
| `FilePath(pattern)` | File path (glob) | `FilePath("*.py")` |
| `Command(pattern)` | Bash command (regex) | `Command(r"git\s+stash")` |
| `Content(pattern)` | File content (regex) | `Content(r"print\(")` |
| `Agent(pattern)` | Subagent type (regex) | `Agent("cleanup-.*")` |
| `TestFile()` | Current file is a test | `TestFile()` |

### Transcript-history conditions

These check what has happened earlier in the session:

| Condition | Matches | Example |
|-----------|---------|---------|
| `ReadFile(pattern)` | A file was read (glob) | `ReadFile("TESTING.md")` |
| `TouchedFile(pattern)` | A file was edited (glob) | `TouchedFile("**/*.py")` |
| `RanCommand(pattern)` | A command was run (regex) | `RanCommand(r"uv\s+run\s+mtest")` |
| `UsedSkill(pattern)` | A skill was invoked (regex) | `UsedSkill("codex")` |
| `InPlanMode()` | Agent is in plan mode | `InPlanMode()` |

### Custom conditions

Implement the `CustomCondition` protocol for arbitrary logic:

```python
from captain_hook import CustomCondition, BaseHookEvent

class HasEnoughEdits(CustomCondition):
    threshold: int = 5

    def check(self, evt: BaseHookEvent) -> bool:
        return len(list(evt.ctx.t.tool_uses.where(name="Edit"))) >= self.threshold
```

## Registration

### Declarative: `hook()`

The most common pattern — register a hook with a message and conditions:

```python
from captain_hook import hook, Event, Tool, Command

hook(
    Event.PreToolUse,
    message="git stash is not allowed; use jj",
    block=True,
    only_if=[Tool("Bash"), Command(r"git\s+stash")],
)
```

Parameters:

- **`events`** — which events to match (required)
- **`message`** — text shown to Claude (required for declarative hooks)
- **`block`** — `True` to block, `False` to warn (default: `False`)
- **`only_if`** / **`skip_if`** — condition lists
- **`max_fires`** — limit how many times this hook fires per session

### Handler: `@app.on()`

For hooks needing complex logic, use a handler function:

```python
from captain_hook import get_current_app, Event, Tool

app = get_current_app()

@app.on(Event.PreToolUse, only_if=[Tool("Bash")])
def no_sudo(evt):
    cmd = evt.tool_input.get("command", "")
    if cmd.startswith("sudo"):
        return evt.block(f"sudo is not allowed: {cmd}")
```

The handler receives the event and returns `HookResult | None`:

- Return `evt.block(message)` to block
- Return `evt.warn(message)` to warn
- Return `evt.allow()` to explicitly allow
- Return `None` to skip (no action)

### Primitives

Convenience wrappers for common patterns. See the [Primitives Guide](primitives.md).

## Dispatch

When Claude Code fires an event, the dispatch pipeline processes it:

1. **Parse** — JSON from stdin is parsed into a typed event object
2. **Match** — each registered hook is checked against `skip_if` then `only_if`
3. **Execute** — matching hooks run their handler (or return their message)
4. **Collect** — first block wins; warnings accumulate
5. **Format** — results are formatted as JSON and written to stdout

The output JSON follows Claude Code's expected format:

```json
{"permissionDecision": "deny", "permissionDecisionReason": "..."}
```

For warnings:

```json
{"permissionDecision": "allow", "additionalContext": "..."}
```
