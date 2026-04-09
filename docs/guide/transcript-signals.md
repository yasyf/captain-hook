# Transcript & Signals

## Transcript

The `Transcript` provides a typed API for querying the conversation history.
Access it via `evt.ctx.t` in hook handlers.

### Loading

```python
from captain_hook import Transcript

# From a JSONL file (how Claude Code stores transcripts)
t = Transcript.from_path("/path/to/transcript.jsonl")

# From raw message dicts
t = Transcript.from_messages([
    {"role": "user", "content": "Fix the bug"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "I'll look into it"},
        {"type": "tool_use", "name": "Bash", "id": "tu1",
         "input": {"command": "pytest tests/ -x"}},
    ]},
])
```

### Querying tool uses

```python
# All tool uses (filters errors by default)
for tu in t.tool_uses:
    print(tu.name, tu.file)

# Include errors
for tu in t.tool_uses.with_errors:
    print(tu.name, tu.is_error)

# Filtered queries
edits = t.tool_uses.where(name="Edit", file="*.py")
bash_cmds = t.tool_uses.where(name="Bash")
errors = t.tool_uses.where(is_error=True)

# Terminal methods
edits.count()          # int
edits.any()            # bool
edits.first()          # ToolUse | None
edits.last()           # ToolUse | None
edits.files()          # list[File]
edits.list()           # list[ToolUse]
```

### Convenience checks

```python
# Has a specific tool been used? (alias-aware: Bash ↔ Execute)
t.has_tool("Bash")

# Has a command matching a regex been run?
t.has_command(r"uv\s+run\s+mtest")

# Has a file matching a glob been edited?
t.has_edit_to("**/*.py")

# Has a file been read?
t.has_read("TESTING.md")

# Did the user say something?
t.user_said("fix", "bug")

# Has a skill been used?
t.has_skill("codex")
```

### Slicing

```python
# Messages after the last use of a tool
after_edit = t.after("Edit")

# Messages before the last use of a tool
before_test = t.before("Bash")

# Last N messages
recent = t.recent(5)

# All messages before the current turn
prior = t.prior
```

### Current turn

```python
turn = t.current_turn
turn.user_text        # The user's message text
turn.edited_files     # Set of File objects edited this turn
```

### Text extraction

```python
t.full_text           # All message text concatenated
t.assistant_text      # Assistant messages only, truncated
t.commands            # Parsed Command objects from Bash uses
```

## Typed tool inputs

Tool uses have typed input objects accessible via `tu.input`:

| Tool | Input type | Key fields |
|------|-----------|------------|
| Bash / Execute | `BashInput` | `command`, `timeout` |
| Edit | `EditInput` | `file_path`, `old`, `new`, `file` |
| Write / Create | `WriteInput` | `file_path`, `content`, `file` |
| Read | `ReadInput` | `file_path`, `limit`, `offset`, `file` |
| Task | `AgentInput` | `agent_type`, `prompt` |
| Grep | `GrepInput` | `pattern`, `file_type`, `path` |
| Glob | `GlobInput` | `patterns` |
| Skill | `SkillInput` | `skill` |
| TaskCreate | `TaskCreateInput` | `description` |
| TaskUpdate | `TaskUpdateInput` | `task_id`, `status` |
| (other) | `GenericInput` | `raw` (the raw dict) |

Use `.as_()` for safe type narrowing:

```python
if bash := tu.input.as_(BashInput):
    print(bash.command)
```

## Signals

Signals score recent transcript text to detect patterns like retry loops,
speculation, or blame-shifting.

### Signal (regex)

```python
from captain_hook import Signal

Signal(pattern=r"let me try again", weight=2)
Signal(pattern=r"retry|retrying", weight=1)
Signal(pattern=r"investigating", weight=-3)  # suppresses score
```

### NlpSignal (NLP-based)

```python
from captain_hook import NlpSignal, Clause, Phrase

NlpSignal(
    clauses=[
        Clause(noun=Phrase("test"), verb=Phrase("run")),
    ],
    weight=3,
)
```

NLP signals use spaCy dependency parsing to match semantic relationships
(e.g., "run the test" matches `verb=run, noun=test`). Phrases can be
expanded with WordNet synonyms via `Phrase("run").expand()`.

### Signals bundle

```python
from captain_hook import Signals, Signal

signals = Signals(
    patterns=[
        Signal(r"retry", weight=2),
        Signal(r"investigating", weight=-3),
    ],
    threshold=3,    # minimum score to trigger
    window=10,      # last N messages to score
)
```

### Using signals with primitives

Pass a `Signals` bundle to `nudge()`, `gate()`, `llm_gate()`, or `llm_nudge()`:

```python
from captain_hook import nudge, Signals, Signal

nudge(
    "Stop retrying — narrow the test first",
    signals=Signals(
        patterns=[Signal(r"try again", weight=2)],
        threshold=3,
        window=10,
    ),
)
```

The primitive automatically:

1. Extracts text from the last `window` transcript messages
2. Scores each text against all patterns
3. Deduplicates via content hashing (same text won't re-trigger)
4. Appends "Triggered by: ..." context to the message
