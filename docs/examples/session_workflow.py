from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Event,
    HookResult,
    Input,
    Tool,
    Waiting,
    on,
    workflow_state,
)
from pydantic import BaseModel, Field


@workflow_state("review")
class ReviewState(BaseModel):
    intent: str | None = None
    ran_tests: bool = False
    edits: list[str] = Field(default_factory=list)


@on(Event.UserPromptSubmit, tests={Input(prompt="refactor the auth module"): Allow()})
def capture_intent(evt: BaseHookEvent) -> HookResult | None:
    state = ReviewState.load(evt)
    state.intent = (evt.user_prompt or "").strip()[:200]
    state.save(evt)
    return None


@on(
    Event.PreToolUse,
    only_if=[Tool("Edit|Write")],
    tests={Input(tool="Edit", file="app/users.py", content="x = 1\n"): Allow()},
)
def record_edit(evt: BaseHookEvent) -> HookResult | None:
    if not (fp := evt.file):
        return None
    state = ReviewState.load(evt)
    state.edits = [*state.edits, str(fp.path)]
    state.save(evt)
    return None


@on(Event.PreToolUse, only_if=[Tool("Bash")], tests={Input(command="pytest -q"): Allow()})
def mark_tested(evt: BaseHookEvent) -> HookResult | None:
    cl = evt.command_line
    if cl and cl.q.runs("pytest"):
        state = ReviewState.load(evt)
        state.ran_tests = True
        state.save(evt)
    return None


@on(Event.Stop, skip_if=[Waiting()], tests={Input(): Allow()})
def require_tests_after_edits(evt: BaseHookEvent) -> HookResult | None:
    state = ReviewState.load(evt)
    if state.edits and not state.ran_tests:
        return evt.block(
            f"You edited {len(state.edits)} file(s) for `{state.intent}` "
            "but never ran tests. Run pytest before stopping."
        )
    return None
