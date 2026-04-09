# Hooks & Events

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
from captain_hook import hook, Event

hook(Event.Stop | Event.SubagentStop, message="Run tests first", block=True)
```

### Event classes

Each `Event` flag maps to a strongly-typed event class:

| Flag | Class | Key properties |
|------|-------|---------------|
| `PreToolUse` | `PreToolUseEvent` | `tool_name`, `tool_input` |
| `PostToolUse` | `PostToolUseEvent` | `tool_name`, `tool_input` |
| `PostToolUseFailure` | `PostToolUseFailureEvent` | `tool_name`, `tool_input`, `error` |
| `Stop` | `StopEvent` | — |
| `SubagentStop` | `SubagentStopEvent` | `agent_type` |
| `SubagentStart` | `SubagentStartEvent` | `agent_type` |
| `UserPromptSubmit` | `UserPromptSubmitEvent` | `prompt` |
| `Notification` | `NotificationEvent` | — |
| `PreCompact` | `PreCompactEvent` | — |

All events provide a `ctx` property returning a `HookContext` with access to transcript, settings, session state, CLI helpers, and LLM invocation.

## Registration

There are three ways to register hooks, from simplest to most flexible.

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

| Parameter | Description |
|-----------|-------------|
| `events` | Which events to match (required) |
| `message` | Text shown to Claude (required for declarative hooks) |
| `block` | `True` to block, `False` to warn (default: `False`) |
| `only_if` / `skip_if` | Condition lists — see [Conditions](conditions.md) |
| `max_fires` | Limit how many times this hook fires per session |

### Primitives

Convenience wrappers for common patterns. One function call handles event targeting, conditions, and defaults:

```python
from captain_hook import block_command, nudge, gate, lint, TouchedFile, RanCommand

block_command(["git", "stash"], reason="Use jj instead")
nudge("Remember to run tests", only_if=[TouchedFile("**/*.py")])
gate("Run tests before stopping", skip_if=[RanCommand(r"pytest")])
lint("no_print", r"print\(", message="Use logger instead")
```

See the [Primitives Guide](primitives.md) for the full list.

### Handler: `@on()`

For hooks needing complex logic, use a handler function:

```python
from captain_hook import on, Event, Tool

@on(Event.PreToolUse, only_if=[Tool("Bash")])
def no_sudo(evt):
    cmd = evt.tool_input.get("command", "")
    if cmd.startswith("sudo"):
        return evt.block(f"sudo is not allowed: {cmd}")
```

The handler receives the event and returns `HookResult | None`:

| Return | Effect |
|--------|--------|
| `evt.block(message)` | Block the action |
| `evt.warn(message)` | Warn (allow with context) |
| `evt.allow()` | Explicitly allow |
| `None` | Skip (no action) |

!!! tip "Which pattern should I use?"
    Start with **primitives** — they cover most hooks in one line. Use **declarative `hook()`** when you need custom condition combinations. Use **handlers** only when you need runtime logic (conditionals, state access, dynamic messages).

## Conditions

Conditions filter *when* a hook fires. See the dedicated [Conditions guide](conditions.md) for the full reference, or the [Conditions Cheatsheet](../reference/conditions.md) for a quick-reference card.

```python
from captain_hook import hook, Event, Tool, Command, RanCommand

hook(
    Event.PreToolUse,
    message="blocked",
    block=True,
    only_if=[Tool("Bash"), Command(r"rm\s+-rf")],  # AND — all must match
    skip_if=[RanCommand(r"confirm-delete")],         # OR — any skips
)
```

## Dispatch pipeline

When Claude Code fires an event, the dispatch pipeline processes it:

1. **Parse** — JSON from stdin is parsed into a typed event object
2. **Match** — each registered hook is checked against `skip_if` then `only_if`
3. **Execute** — matching hooks run their handler (or return their message)
4. **Collect** — first block wins; warnings accumulate
5. **Format** — results are formatted as JSON and written to stdout

See [How It Works](../getting-started/how-it-works.md) for a detailed step-by-step walkthrough with actual JSON input/output.
