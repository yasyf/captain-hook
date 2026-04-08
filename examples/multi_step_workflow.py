# Example: Multi-step workflow
#
# Demonstrates how to use `workflow`, `Step`, and `text_matches` to
# enforce a multi-step process.  The workflow blocks SubagentStop
# until each step's check predicate passes against the transcript
# and a completion marker is found.

from captain_hook import Step, text_matches, workflow

# Register a 3-step deployment workflow.  The workflow guard
# fires on SubagentStop and blocks until all steps pass.
workflow(
    label="DEPLOY",
    marker="DEPLOY COMPLETE",
    steps=[
        Step(
            name="run tests",
            check=text_matches(r"pytest.*passed"),
            stopped_at="Stop: tests not run.",
            next_step="Run the test suite with pytest.",
        ),
        Step(
            name="build artifacts",
            check=text_matches(r"build\s+succeeded"),
            stopped_at="Stop: build not completed.",
            next_step="Run the build step.",
        ),
        Step(
            name="deploy",
            check=text_matches(r"deployed\s+to\s+production"),
            stopped_at="Stop: not deployed.",
            next_step="Deploy to production.",
        ),
    ],
)
