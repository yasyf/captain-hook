# Workflows

*Enforce a multi-step checklist before a subagent can stop.*

## The problem

Before a subagent finishes its task, it must run tests, pass linting, and produce a coverage report. But Claude sometimes decides it is "done" after implementing the feature, skipping verification entirely. You need a way to enforce the checklist so the subagent cannot stop until every step is complete.

## The solution

### Define the workflow

A `workflow` registers a guard on `SubagentStop` that checks transcript history for evidence of each step. If any step is incomplete, the agent is blocked with instructions for what to do next.

```python
from captain_hook import workflow, Step, text_matches

workflow(
    label="VERIFY",
    marker="VERIFY COMPLETE",
    steps=[
        Step(
            name="run tests",
            check=text_matches(r"uv run mtest.*passed"),
            stopped_at="Stop: tests not run.",
            next_step="Run the test suite: uv run mtest",
        ),
        Step(
            name="run linter",
            check=text_matches(r"ruff check.*passed|no issues found"),
            stopped_at="Stop: linter not run.",
            next_step="Run the linter: ruff check .",
        ),
        Step(
            name="confirm coverage",
            check=text_matches(r"coverage.*\d+%"),
            stopped_at="Stop: coverage not checked.",
            next_step="Check coverage: uv run mtest --coverage",
        ),
    ],
)
```

### How it works

The workflow guard checks three things in order:

1. **Steps**: Each `Step` has a `check` function that scans the transcript. `text_matches(pattern)` returns a function that searches the full transcript text for a regex match.

2. **Marker**: The string `"VERIFY COMPLETE"` must appear somewhere in the transcript. This is a deliberate signal the agent writes when it believes all steps are done.

3. **Blocking**: If any step fails or the marker is missing, the guard returns a block result with the `stopped_at` message and the `next_step` instruction.

### What the agent sees when it tries to skip

If the agent tries to stop after running tests but before running the linter:

```
VERIFY INCOMPLETE: Stop: linter not run. Run the linter: ruff check .
```

The agent cannot finish until it runs the linter, sees the output match the check pattern, and then the coverage step as well. Only after all checks pass and the marker appears does the guard allow the stop.

### Adding artifact validation

Workflows can also validate output files. Use `Artifact` to require that a file exists and contains valid data:

```python
from pydantic import BaseModel
from captain_hook import workflow, Step, Artifact, text_matches

class CoverageReport(BaseModel):
    total: float
    files: dict[str, float]

workflow(
    label="VERIFY",
    marker="VERIFY COMPLETE",
    steps=[
        Step(
            name="run tests",
            check=text_matches(r"uv run mtest.*passed"),
            stopped_at="Stop: tests not run.",
            next_step="Run: uv run mtest",
        ),
    ],
    artifacts=[
        Artifact(
            path=".coverage/report.json",
            model=CoverageReport,
            validate=lambda r: "Coverage below 80%" if r.total < 80 else None,
        ),
    ],
)
```

Artifact validation runs after all steps pass:

1. The file must exist at the specified path.
2. The file content must parse as valid JSON matching the Pydantic model.
3. The `validate` function runs on the parsed model. Return `None` to pass or a string to fail.

If any artifact check fails:

```
VERIFY INCOMPLETE: .coverage/report.json not found.
```

Or if the coverage is too low:

```
VERIFY INCOMPLETE: Coverage below 80%
```

### Full example with steps and artifacts

```python
from pydantic import BaseModel
from captain_hook import workflow, Step, Artifact, text_matches

class LintReport(BaseModel):
    errors: int
    warnings: int

class TestReport(BaseModel):
    passed: int
    failed: int
    skipped: int

workflow(
    label="QA",
    marker="QA COMPLETE",
    steps=[
        Step(
            name="tests",
            check=text_matches(r"(\d+) passed"),
            stopped_at="Stop: tests not run.",
            next_step="Run: uv run mtest",
        ),
        Step(
            name="lint",
            check=text_matches(r"lint.*passed|no issues"),
            stopped_at="Stop: lint not run.",
            next_step="Run: ruff check .",
        ),
        Step(
            name="type check",
            check=text_matches(r"mypy.*success|no errors"),
            stopped_at="Stop: type check not run.",
            next_step="Run: mypy .",
        ),
    ],
    artifacts=[
        Artifact(
            path=".qa/test-report.json",
            model=TestReport,
            validate=lambda r: "Tests failed" if r.failed > 0 else None,
        ),
        Artifact(
            path=".qa/lint-report.json",
            model=LintReport,
            validate=lambda r: "Lint errors found" if r.errors > 0 else None,
        ),
    ],
)
```

!!! note
    The workflow guard fires once on `SubagentStop` (`max_fires=1`). If it blocks, the subagent continues running and will trigger the guard again on its next stop attempt. This continues until all steps and artifacts pass.

!!! tip
    The `marker` string serves as a deliberate completion signal. Tell the agent in its task instructions to write "QA COMPLETE" when it has finished all verification steps. This prevents accidental passes where test output happens to match a step pattern from a previous run.
