from __future__ import annotations

from pydantic import BaseModel

from captain_hook import Artifact, Step, text_matches, workflow


class TestReport(BaseModel):
    passed: int
    failed: int


workflow(
    label="VERIFY",
    marker="VERIFY COMPLETE",
    steps=[
        Step(
            name="run tests",
            check=text_matches(r"pytest.*passed"),
            stopped_at="Stop: tests not run.",
            next_step="Run the test suite with pytest.",
        ),
        Step(
            name="run linter",
            check=text_matches(r"ruff check.*passed|no issues found"),
            stopped_at="Stop: linter not run.",
            next_step="Run: ruff check .",
        ),
        Step(
            name="confirm coverage",
            check=text_matches(r"coverage:\s*\d+%"),
            stopped_at="Stop: coverage not checked.",
            next_step="Check coverage and print `coverage: NN%`.",
        ),
    ],
    artifacts=[
        Artifact(
            path=".reports/tests.json",
            model=TestReport,
            validate=lambda r: f"{r.failed} tests failed" if r.failed else None,
        ),
    ],
)
