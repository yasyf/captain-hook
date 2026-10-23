from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    CustomCondition,
    Event,
    FreshSession,
    FromSubagent,
    HookResult,
    Input,
    Prompt,
    Warn,
    on,
)
from captain_hook.util import reqenv

PRELOADED = (
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "Monitor",
    "SendMessage",
    "EnterPlanMode",
    "ExitPlanMode",
)


class CodexSession(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        return reqenv.provider() == "codex"


@on(
    Event.SessionStart,
    only_if=[FreshSession()],
    skip_if=[FromSubagent(), CodexSession()],
    max_fires=1,
    tests={
        Input(source="startup"): Warn(pattern="select:TaskCreate"),
        Input(source="clear"): Warn(pattern="select:TaskCreate"),
        Input(source="resume"): Allow(),
        Input(source="compact"): Allow(),
        Input(source="startup", agent_id="sub-1"): Allow(),
        Input(source="clear", agent_id="sub-1"): Allow(),
        Input(source="startup", disallowed_tools=("AskUserQuestion",)): Warn(
            pattern="select:TaskCreate,TaskGet,TaskList,TaskStop,TaskUpdate,Monitor,SendMessage,"
            "EnterPlanMode,ExitPlanMode` and `max_results` 9"
        ),
        Input(source="startup", disallowed_tools=("AskUserQuestion", "EnterPlanMode", "ExitPlanMode")): Warn(
            pattern="select:TaskCreate,TaskGet,TaskList,TaskStop,TaskUpdate,Monitor,SendMessage` and `max_results` 7"
        ),
        Input(source="startup", disallowed_tools=PRELOADED): Allow(),
        Input(source="startup", env={"CAPT_HOOK_PROVIDER": "codex"}): Allow(),
        Input(source="clear", env={"CAPT_HOOK_PROVIDER": "codex"}): Allow(),
    },
)
def preload_tools(evt: BaseHookEvent) -> HookResult | None:
    if not (tools := [name for name in PRELOADED if name not in evt.disallowed_tools]):
        return None
    return evt.context(str(Prompt.load("tools/preload_nudge", tools=",".join(tools), count=len(tools))))
