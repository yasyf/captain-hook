# Testing Hooks

Every primitive accepts `tests=` — a dict mapping `Input` descriptors to expected outcomes.
`uvx --isolated capt-hook test` discovers all hooks, builds a mock event from each `Input`, runs the
hook's conditions and handler, and asserts the outcome. Exit code 1 on any failure.

## How a test runs

1. The runner builds the event from the hook's **first** registered event flag — a `gate`
   on `Stop | SubagentStop` is tested as a `Stop` event, so `Input(command=...)` is ignored
   there; describe history via `transcript=` instead.
2. When `Input.tool` is unset, the tool comes from the hook's first `Tool` condition
   (first alternative of the pattern: `Tool("Edit|Write")` infers `Edit`), else `Bash`.
3. `skip_if`/`only_if` are evaluated; a non-matching input yields `Allow()`.
4. LLM calls are stubbed: `GateVerdict(block=True)`, `NudgeVerdict(fire=True)`,
   `PromptCheckVerdict(action="block")`. The default stub always fires, so inline tests on
   LLM hooks assert only conditions and wiring, never judgment — ship LLM hooks without
   `tests=` unless a required `contexts=` provider makes them meaningful.
   `Input(llm={"fire": False})` / `Input(llm={"block": False})` overrides the stub verdict
   per test, wire-testing the judge-declines path. `evt.ctx.diff` is stubbed to a canned
   non-empty diff, so `diff=True` hooks stay deterministic instead of reading the real
   working tree.

## Input fields

| Field | Models | Example |
|---|---|---|
| `command` | Bash tool command | `Input(command="git stash")` |
| `file` | Edit/Write/Read file path | `Input(file="src/main.py")` |
| `content` | Write content / Edit new string | `Input(file="x.py", content="print('hi')")` |
| `old` | Edit old string | `Input(file="x.py", old="foo", content="bar")` |
| `tool` | Override tool name | `Input(tool="Write", file="x.py", content="...")` |
| `prompt` | UserPromptSubmit text | `Input(prompt="Fix the bug")` |
| `agent_type` | Subagent type | `Input(agent_type="cleanup")` |
| `agent_id` | Subagent/teammate id; presence makes `evt.is_subagent` and `FromSubagent()` true | `Input(command="ls", agent_id="tm1")` |
| `permission_mode` | e.g. plan mode | `Input(permission_mode="plan")` |
| `skip_permissions` | Pre-seeds `evt.skip_permissions` / `SkipPermissions()`; `None` leaves the real process-tree walk in place | `Input(command="ls", skip_permissions=True)` |
| `transcript` | Session history | `Input(transcript=TranscriptFixture(messages=[...]))` |

## Expected outcomes

All fields are keyword-only — `Block(pattern="...")`, never `Block("...")`.

- `Block(pattern=None)` — the hook must block; optional regex searched in the block message.
- `Warn(pattern=None)` — the hook must warn; optional regex searched in the warning.
- `Allow()` — the hook must allow (return `None` or action `"allow"`);
  `Allow(explicit=True)` requires an actual allow result, so `None` fails.
- `Ask()` — the hook must return no result (`None`); for `PermissionRequest` hooks that
  means the dialog shows.

## TranscriptFixture recipe

Transcript-history conditions (`TouchedFile`, `RanCommand`, `ReadFile`, `UsedSkill`) read
the session transcript, so their tests supply one. The message shape is Claude Code JSONL:
`{"type": "assistant", "message": {"content": [<tool_use blocks>]}}`.

```python
EDITED_THEN_TESTED = TranscriptFixture(messages=[
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "id": "t1",
         "input": {"file_path": "src/main.py", "old_string": "a", "new_string": "b"}},
        {"type": "tool_use", "name": "Bash", "id": "t2",
         "input": {"command": "uv run pytest"}},
    ]}},
])
```

A gate's three canonical tests: evidence-only fixture expects `Block`, evidence-plus-remedy
fixture expects `Allow()` (the `skip_if` matched), and bare `Input()` expects `Allow()` (no
evidence, `only_if` fails).

## Output formats

`uvx --isolated capt-hook test` prints one line per test and a summary:

```
  PASS  workflow:gate_50b992e3:Input(command='git push origin main', ...)
  FAIL  testing:gate_2e34ce56:Input(...): [testing:gate_2e34ce56] Expected Block, got None

18 tests: 17 passed, 1 failed, 0 errors, 0 skipped
```

`uvx --isolated capt-hook test --json` emits one record per test for parsing:

```json
{"id": "workflow:gate_50b992e3:Input(...)", "status": "pass", "expected": "block", "reason": ""}
```

Statuses: `pass`, `fail` (wrong outcome), `error` (exception in hook or test), `skip`
(legacy string-key tests with no recorded fixture).

## Debugging failures

| Symptom | Likely cause and fix |
|---|---|
| `Expected Block, got None` on a Stop gate | The `Input` lacks transcript evidence for `only_if` — add a `TranscriptFixture` whose tool uses satisfy `TouchedFile`/`RanCommand`. Check the fixture shape: `type` + nested `message.content`, not `role`/`content`. |
| `Expected Block, got None` on a glob condition | `src/**/*.py` does not match `src/main.py` — the `**` wants an intermediate directory. Use `**/*.py` or `src/*.py`. |
| `Block message doesn't match '<pat>'` | `pattern` is regex-searched against the *rendered* message. `block_command` renders `BLOCKED: {reason}. {hint}.` — pick a word that survives rendering, or loosen the pattern. |
| `@on` handler on a piped command never fires | `evt.command_line.q.runs(...)` checks the pipeline's *last* command. Use `.any_command(lambda c: c.program == "...")`. |
| Edit/Write hook gets `None` content | The `Input` set `command=` but the hook's first event/tool is Edit — set `file=` and `content=` (and `tool=` if the hook matches several). |
| `Input(command=...)` ignored | The hook's first event is `Stop` — the runner builds a Stop event without tool input. Encode the command as a Bash `tool_use` in `transcript=` instead. |
| LLM hook test always blocks | The default stub verdict always fires. Pass `Input(llm={"fire": False})` / `{"block": False}` to exercise the declines path, or remove `tests=` and test the static `only_if` narrowing on a deterministic sibling hook. |
