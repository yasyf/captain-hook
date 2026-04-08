# Testing Guide

## Inline tests

Every primitive supports a `tests` parameter — a dict mapping `Input` to
expected outcomes (`Block`, `Warn`, or `Allow`):

```python
from captain_hook import block_command, Input, Block, Allow

block_command(
    ["git", "stash"],
    reason="Use jj instead",
    tests={
        Input(command="git stash"): Block("jj"),
        Input(command="git status"): Allow(),
    },
)
```

Run all inline tests:

```bash
python -m captain_hook test --hooks src/
```

### Input fields

`Input` models the event payload. Which fields you set depends on the event:

| Field | Event type | Example |
|-------|-----------|---------|
| `command` | Bash/Execute tool | `Input(command="git stash")` |
| `file` | Edit/Write file path | `Input(file="src/main.py")` |
| `content` | Write content / Edit new | `Input(file="x.py", content="print('hi')")` |
| `old` | Edit old content | `Input(file="x.py", old="foo", content="bar")` |
| `tool` | Override tool name | `Input(tool="Grep")` |
| `prompt` | UserPromptSubmit | `Input(prompt="Fix the bug")` |
| `agent_type` | Subagent type | `Input(agent_type="cleanup")` |
| `transcript` | Session transcript | `Input(transcript=Path("fixture.jsonl"))` |

### Expected outcomes

- **`Block(pattern?)`** — expects the hook to block. Optional regex pattern matches the block message.
- **`Warn(pattern?)`** — expects the hook to warn. Optional regex pattern matches the warning.
- **`Allow()`** — expects the hook to allow (return `None` or action `"allow"`).

## mock_event

Create mock events for unit tests:

```python
from captain_hook import mock_event, mock_tool_event, mock_stop_event

# Generic mock — infers event type from tool name
evt = mock_event(tool="Bash", command="ls -la")

# Specific factories
evt = mock_tool_event("Edit", file_path="src/main.py", old_string="foo", new_string="bar")
evt = mock_stop_event()
evt = mock_subagent_stop_event(agent_type="cleanup")
evt = mock_user_prompt_event(prompt="Fix the tests")
```

## dispatch_test

Round-trip test through the full dispatch pipeline:

```python
from captain_hook import dispatch_test, HookApp, Event, hook, Command, Input

app = HookApp()
with app:
    hook(Event.PreToolUse, message="blocked", block=True, only_if=[Command(r"rm")])

result = dispatch_test(app, Input(command="rm -rf /"))
assert result is not None
assert result["permissionDecision"] == "deny"
```

## assert_result

Validate dispatch results against expected outcomes:

```python
from captain_hook import assert_result, Block, Warn, Allow

# Passes — result is a block with matching message
assert_result({"permissionDecision": "deny", "permissionDecisionReason": "blocked"}, Block("blocked"))

# Passes — result is None (allowed)
assert_result(None, Allow())

# Raises AssertionError — expected block but got allow
assert_result(None, Block("should have blocked"))
```

## pytest integration

Write standard pytest tests using the mock helpers:

```python
import pytest
from captain_hook import HookApp, Event, hook, mock_event, Command

def test_rm_blocked():
    app = HookApp()
    with app:
        hook(Event.PreToolUse, message="not allowed", block=True,
             only_if=[Command(r"rm\s+-rf")])

    evt = mock_event(tool="Bash", command="rm -rf /")
    from captain_hook import dispatch
    results = dispatch(app, evt)
    assert any(r.action.value == "block" for r in results)

def test_ls_allowed():
    app = HookApp()
    with app:
        hook(Event.PreToolUse, message="not allowed", block=True,
             only_if=[Command(r"rm\s+-rf")])

    evt = mock_event(tool="Bash", command="ls -la")
    from captain_hook import dispatch
    results = dispatch(app, evt)
    assert not any(r.action.value == "block" for r in results)
```

## TranscriptFixture

For hooks that depend on transcript context, use `TranscriptFixture`:

```python
from captain_hook import Input, Block, Allow, Warn, TranscriptFixture, nudge, TouchedFile, RanCommand

nudge(
    "Run tests after editing",
    only_if=[TouchedFile("**/*.py")],
    skip_if=[RanCommand(r"pytest")],
    tests={
        Input(command="echo hi", transcript=TranscriptFixture(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "id": "t1",
                 "input": {"file_path": "src/main.py", "old_string": "a", "new_string": "b"}},
            ]},
        ])): Warn(),
    },
)
```
