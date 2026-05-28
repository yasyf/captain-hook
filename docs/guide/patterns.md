# Real-World Patterns

Patterns harvested from a production hooks directory. Each is one concrete answer to a recurring problem, with enough surrounding code to copy and adapt.

## Block dangerous VCS ops

**Problem:** the agent reaches for `git stash`, `git push --force`, `jj abandon`, or other history-mutating commands that you want a human to authorize.

```python
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "stash"],
    reason="Use jj shelve or commit a WIP change instead",
    tests={Input(command="git stash"): Block(), Input(command="git status"): Allow()},
)

block_command(
    r"git\s+push\s+--force(?!-)",
    reason="Force-push rewrites remote history",
    hint="Use --force-with-lease for a safer push",
)
```

**When to use:** `block_command` accepts a token list (matched as an argv prefix) or a raw regex. Reach for the regex form whenever you need a negative lookahead or capture group — anything richer than "starts with these arguments".

## prompt-check for weakened tests

**Problem:** an agent fixes a failing test by deleting the assertion, mocking the integration boundary, or marking the file `@pytest.mark.skip`. You want an LLM to look at the diff and refuse the edit if it weakens the test.

```python
from captain_hook import Event, Prompt, SourceEdits, TestFile, Tool, on, prompt_check

INTEGRITY = """
Block if the edit replaces an assertion with `assert True`, swaps a real call
for a Mock, or adds @pytest.mark.skip without justification.
File: {fp}
--- old ---
{old}
--- new ---
{new}
"""

@on(Event.PostToolUse, only_if=[SourceEdits(lang="py", include_tests=True), TestFile(), Tool("Edit")])
def guard_test_edits(evt):
    if not (fp := evt.file) or not (old := evt.old) or not (new := evt.content):
        return None
    return prompt_check(evt, Prompt.from_template(INTEGRITY, fp=fp.path, old=old, new=new), prefix="TEST INTEGRITY")
```

**When to use:** `prompt_check` is the right call when the decision needs reasoning over diffs rather than a regex. Combine with `Prompt.from_template` so the template lives next to the hook, not as a string concatenation.

## Session-state workflow

**Problem:** you want a hook that spans multiple events — capture intent from the user's prompt, record edits as they happen, then gate `Stop` if the agent never ran tests.

```python
from captain_hook import Event, Tool, Waiting, on, workflow_state
from pydantic import BaseModel, Field

@workflow_state("review")
class ReviewState(BaseModel):
    intent: str | None = None
    ran_tests: bool = False
    edits: list[str] = Field(default_factory=list)

@on(Event.UserPromptSubmit)
def capture_intent(evt):
    state = ReviewState.load(evt)
    state.intent = (evt.prompt or "").strip()[:200]
    state.save(evt)

@on(Event.Stop, skip_if=[Waiting()])
def require_tests(evt):
    state = ReviewState.load(evt)
    if state.edits and not state.ran_tests:
        return evt.block(f"Edited {len(state.edits)} files but never ran tests.")
```

**When to use:** `@workflow_state("key")` registers one Pydantic model that every event handler can `.load(evt)` / `.save(evt)` against. Prefer it over a bag of standalone `@session_state` models whenever the data is conceptually one workflow.

## Echo-suppressed nudge cluster

**Problem:** signal-driven nudges fire correctly the first time, then the agent ignores them and the same nudge keeps firing on every subsequent tool call. You want them to fire once per cluster, not on every event.

```python
import re
from captain_hook import Event, ReadFile, Signal, Signals, UsedSkill, nudge

RETRY = Signals(
    patterns=[
        Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"\bretrying\b", weight=1, flags=re.IGNORECASE),
    ],
    threshold=4,
    window=10,
)

nudge(
    "Repeated failures detected. Stop retrying and pick a debug tool.",
    signals=RETRY,
    events=Event.Stop,
    skip_if=[UsedSkill("codex"), ReadFile("DEBUGGING.md")],
    max_fires=1,
)
```

**When to use:** `Signals` scores recent transcript text and only fires once the threshold lands. Pair with `max_fires` and `skip_if=[UsedSkill(...), ReadFile(...)]` so the nudge bows out once the agent acts on it.

## AST lint with LLM escalation

**Problem:** you want a fast regex / AST check to catch the obvious case and an LLM to gate the ambiguous case. The pair should layer: regex blocks on the clear violation; LLM only runs when regex isn't sure.

```python
import ast
from captain_hook import Content, Event, SourceEdits, TestFile, hook, lint, llm_gate

hook(
    Event.PostToolUse,
    only_if=[SourceEdits(lang="py"), Content(r"^\s*print\(")],
    skip_if=[TestFile()],
    message="Use the project logger instead of print().",
)

def bare_excepts(node):
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        yield f"line {node.lineno}: bare except"

lint(bare_excepts, message="Bare except: {violations}", trigger="except")

llm_gate(
    "Does this diff add a prod print() that should be a logger call? Block only if unambiguous.",
    message=lambda r: f"Replace print() with logger: {r.reasoning}",
    only_if=[SourceEdits(lang="py"), Content(r"^\s*print\(")],
    max_fires=2,
)
```

**When to use:** the regex `hook` covers the static case for free; `lint` walks the AST so you can match by structure; `llm_gate` only spends a model call when the cheaper layers can't decide. Cap LLM cost with `max_fires`.

## Audit log for an agent run

**Problem:** you want a JSONL log of every tool call, file edit, and stop event, partitioned by hour and filtered to one slice of activity.

```python
from captain_hook import Event, FilePath, audit

audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)

audit(
    Event.PostToolUse,
    log_dir="logs/hooks",
    filename=lambda d: f"{d:%Y-%m-%dT%H}.jsonl",
    fields=lambda evt: {"event": evt.event_name.name, "tool": evt.tool_name, "file": str(evt.file.path) if evt.file else None},
    only_if=[FilePath("**/*.py")],
)
```

**When to use:** `audit()` never blocks the agent — it only writes JSONL. Use the bare form for general capture; the second form when you need custom fields, custom destination, or per-slice filtering.

## Workflow checklist with artifact validation

**Problem:** before the agent stops, it must (1) run tests, (2) run a linter, (3) write a coverage line, and (4) drop a structured report at a known path. Any missing step should block `Stop`.

```python
from captain_hook import Artifact, Step, text_matches, workflow
from pydantic import BaseModel

class TestReport(BaseModel):
    passed: int
    failed: int

workflow(
    label="VERIFY",
    marker="VERIFY COMPLETE",
    steps=[
        Step(name="run tests", check=text_matches(r"pytest.*passed"), stopped_at="Stop: tests not run."),
        Step(name="run linter", check=text_matches(r"ruff check.*passed"), stopped_at="Stop: linter not run."),
    ],
    artifacts=[
        Artifact(
            path=".reports/tests.json",
            model=TestReport,
            validate=lambda r: f"{r.failed} tests failed" if r.failed else None,
        ),
    ],
)
```

**When to use:** any time the gate is "the agent must produce X, Y, Z before it can stop". `Step.check` looks for evidence in the transcript; `Artifact` validates a structured file. The workflow won't release the agent until the marker line appears and every step plus every artifact passes.
