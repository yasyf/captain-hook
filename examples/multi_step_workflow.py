# Example: Multi-step workflow
#
# Demonstrates how to use `workflow`, `Step`, and `text_matches` to
# enforce a multi-step process.  The workflow blocks until each step's
# check predicate passes against the transcript and a completion
# marker is found.

from captain_hook import (
    HookApp,
    Step,
    Transcript,
    Workflow,
    mock_subagent_stop_event,
    text_matches,
    workflow,
)
from captain_hook.app import _current_app


def _msg(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


app = HookApp()
token = _current_app.set(app)

wf = workflow(
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

_current_app.reset(token)

assert isinstance(wf, Workflow)
assert wf.label == "DEPLOY"
assert len(wf.steps) == 3

incomplete_transcript = Transcript.from_messages([
    _msg("I checked the code but haven't run tests yet."),
])
evt_incomplete = mock_subagent_stop_event(transcript=incomplete_transcript)
result = wf.guard(evt_incomplete)
assert result is not None
assert result.action == "block"
assert "tests not run" in result.message.lower()

partial_transcript = Transcript.from_messages([
    _msg("pytest: 42 passed in 3.1s"),
    _msg("Still need to build."),
])
evt_partial = mock_subagent_stop_event(transcript=partial_transcript)
result_partial = wf.guard(evt_partial)
assert result_partial is not None
assert result_partial.action == "block"
assert "build" in result_partial.message.lower()

complete_transcript = Transcript.from_messages([
    _msg("pytest: 42 passed in 3.1s"),
    _msg("build succeeded"),
    _msg("deployed to production"),
    _msg("DEPLOY COMPLETE"),
])
evt_complete = mock_subagent_stop_event(transcript=complete_transcript)
result_complete = wf.guard(evt_complete)
assert result_complete is None

print("✓ Workflow blocked incomplete steps, allowed complete workflow")
