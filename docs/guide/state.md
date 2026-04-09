# State & Sessions

Hooks are stateless by default -- each invocation is independent. Session state lets you persist data across hook invocations within a single Claude Code session, enabling patterns like counting events, tracking what has been seen, and accumulating context.

## Why state

Some hooks need memory:

- **Fire counting** -- block on the 3rd retry, not the 1st
- **Deduplication** -- don't warn about the same pattern twice
- **Accumulation** -- collect file paths across multiple edits, then validate at stop time
- **Computed values** -- cache expensive calculations (e.g., AST analysis results) across events

Conditions check transcript history (what tools were used, what files were read). State stores arbitrary computed values that survive across hook invocations.

## Accessing state

State is accessed through the `SessionStore` on the hook context. Every event provides `evt.ctx.session` (or the shorthand aliases `evt.ctx.s` and `evt.ctx.state`).

The store is keyed by Pydantic model type. You access a slot with bracket syntax:

```python
from captain_hook import on, Event

@on(Event.PostToolUse)
def track_edits(evt):
    slot = evt.ctx.s[EditTracker]  # returns a SessionSlot[EditTracker]
```

## SessionSlot

`SessionSlot[T]` is the interface for reading and writing a single state model. It is parameterized by the Pydantic model type used as the key.

### .get()

Returns the stored instance, or `None` if no state has been written yet. Accepts an optional default:

```python
slot = evt.ctx.s[MyState]

# Returns MyState instance or None
state = slot.get()

# Returns MyState instance or the provided default
state = slot.get(MyState())
```

### .set()

Persists the model instance to disk. The state is written atomically (via temp file + rename) to prevent corruption.

```python
state = MyState(counter=5)
evt.ctx.s[MyState].set(state)
```

### .delete()

Removes the persisted state file:

```python
evt.ctx.s[MyState].delete()
```

## Get-or-create pattern

The most common pattern: read existing state, apply updates, write it back.

```python
from pydantic import BaseModel
from captain_hook import on, Event

class EditCounter(BaseModel):
    count: int = 0
    files: list[str] = []

@on(Event.PostToolUse)
def count_edits(evt):
    state = evt.ctx.s[EditCounter].get() or EditCounter()
    if evt.file:
        state.count += 1
        state.files.append(str(evt.file.path))
    evt.ctx.s[EditCounter].set(state)
```

!!! note "Mutable defaults"
    Use `Field(default_factory=list)` for mutable defaults on Pydantic models. The example above uses `list[str] = []` for brevity, but in production code prefer `files: list[str] = Field(default_factory=list)`.

## Custom state models

Any Pydantic `BaseModel` can be used as a state key. Define your model and use it directly:

```python
from pydantic import BaseModel, Field
from captain_hook import on, Event, HookResult, Action

class ReviewState(BaseModel):
    files_reviewed: set[str] = Field(default_factory=set)
    issues_found: int = 0

@on(Event.PostToolUse)
def track_review(evt):
    if not evt.file:
        return None
    state = evt.ctx.s[ReviewState].get() or ReviewState()
    state.files_reviewed.add(str(evt.file.path))
    evt.ctx.s[ReviewState].set(state)
    return None

@on(Event.Stop)
def review_gate(evt):
    state = evt.ctx.s[ReviewState].get()
    if not state or len(state.files_reviewed) < 3:
        return HookResult(
            action=Action.block,
            message="Review at least 3 files before stopping.",
        )
    return None
```

State is serialized as JSON and stored in the session directory at `~/.claude/state/hooks/sessions/<hash>/<model_key>.json`. The model key is derived from the class name (`ReviewState` becomes `review_state.json`).

## Built-in state models

### HookState

Tracks how many times a specific hook has fired. Used internally by the framework for `max_fires` enforcement.

```python
from captain_hook import HookState

class HookState(BaseModel):
    fire_count: int = 0
```

You rarely need to interact with `HookState` directly -- the framework manages it. But you can read it to inspect fire counts:

```python
from captain_hook import on, Event, HookState

@on(Event.PostToolUse)
def check_activity(evt):
    hs = evt.ctx.s[HookState].get()
    if hs:
        print(f"Hooks have fired {hs.fire_count} times this session")
```

### PrimitiveState

Used internally by LLM primitives (`llm_gate`, `llm_nudge`) for:

- **Signal deduplication** -- tracking which transcript texts have already been scored, so the same text does not re-trigger a signal
- **Turn-level fire suppression** -- preventing multiple LLM primitives from firing in the same dispatch cycle
- **Echo suppression** -- using NLP lemmatization to detect when the agent's response echoes the hook's own message (avoiding feedback loops)

```python
from captain_hook import PrimitiveState

class PrimitiveState(BaseModel):
    last_fired_at: int = 0
    consumed: set[str] = Field(default_factory=set)
    echo_lemmas: set[str] = Field(default_factory=set)
    echo_window_end: int = 0
```

You should not modify `PrimitiveState` directly. It is managed by the LLM primitive internals.

## State vs conditions

Both state and transcript-history conditions can answer "has something happened?" -- but they serve different purposes:

| | Conditions | State |
|---|-----------|-------|
| **Data source** | Transcript (tool uses, messages) | Custom Pydantic models |
| **Persistence** | Recomputed on every event | Written to disk, survives across events |
| **Flexibility** | Fixed set of checks (file read, command run, etc.) | Arbitrary computed values |
| **Use case** | "Was `mtest` run?" "Was this file read?" | "How many times has X happened?" "What was the last computed score?" |

!!! tip "Choose conditions first"
    If your question can be answered by inspecting the transcript ("did the agent run tests?"), use a condition like `RanCommand`. Reserve state for values that cannot be derived from the transcript alone.

## Session lifecycle

State is scoped to a Claude Code session. Each session gets a directory under `~/.claude/state/hooks/sessions/` identified by a hash of the transcript path. When a session's transcript file is deleted, the state directory is cleaned up automatically on the next run.

The state directory can be overridden by setting the `CLAUDE_HOOKS_STATE_DIR` environment variable.
