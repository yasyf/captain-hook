from __future__ import annotations

import re

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    InPlanMode,
    Input,
    Signal,
    Signals,
    Tool,
    Waiting,
    Warn,
    gate,
    nudge,
)

OVERRIDE_TOKEN = "REMAINING_TASKS_ACKNOWLEDGED"
TASK_DRIFT_THRESHOLD = 8

EXPLORATION_TOOLS = "Bash|Grep|Glob|WebSearch|WebFetch|LSP|Skill"

IMPERATIVES = (
    r"\b(?:add|fix|update|change|remove|create|implement|refactor|"
    r"move|rename|delete|replace|extract|split|merge|convert|migrate)\b"
)


class TasksIncomplete(CustomCondition):
    """Matches when the session's native task store has any open (pending/in_progress) task."""

    def check(self, evt: BaseHookEvent) -> bool:
        return not evt.tasks.all_completed


class Acknowledged(CustomCondition):
    """Matches when the agent emitted the override token and hasn't edited since."""

    def __init__(self, token: str) -> None:
        self.token = token

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.ctx.t.has_override(self.token)


class DriftedFromTasks(CustomCondition):
    """Matches when there are open tasks and many exploration calls since the last task touch."""

    def check(self, evt: BaseHookEvent) -> bool:
        if not evt.tasks.open:
            return False
        since = evt.ctx.t.after(tool="TaskCreate|TaskUpdate|TaskList|TaskGet")
        return since.tool_uses.where(name=EXPLORATION_TOOLS).count() >= TASK_DRIFT_THRESHOLD


gate(
    "Open tasks remain. Before stopping, mark each finished task status='completed' via the "
    "TaskUpdate tool (add a note if you're deliberately deferring one), or output "
    f"{OVERRIDE_TOKEN} to acknowledge and stop. See: CLAUDE.md § Task Tracking.",
    only_if=[TasksIncomplete()],
    skip_if=[Waiting(), Acknowledged(OVERRIDE_TOKEN)],
    events=Event.Stop,
    tests={
        Input(tasks=[{"id": "1", "subject": "a", "status": "completed"}]): Allow(),
        Input(tasks=[{"id": "1", "subject": "a", "status": "pending"}]): Block(),
        Input(
            tasks=[{"id": "1", "subject": "a", "status": "pending"}],
            transcript=[{"type": "assistant", "message": {"content": [{"type": "text", "text": OVERRIDE_TOKEN}]}}],
        ): Allow(),
    },
)


nudge(
    "Many exploration/action calls since you last touched the task list. If you discovered "
    "new work or changed direction, use the TaskCreate/TaskUpdate tools to update it. "
    "See: CLAUDE.md § Task Tracking.",
    only_if=[Tool("Edit|Write"), DriftedFromTasks()],
    events=Event.PostToolUse,
    tests={
        Input(file="m.py", content="x = 1\n", tasks=[]): Allow(),
        Input(
            file="m.py",
            content="x = 1\n",
            tasks=[{"id": "1", "subject": "a", "status": "in_progress"}],
            transcript=[
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "TaskCreate", "input": {}, "id": "t1"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": f"b{i}"}
                            for i in range(TASK_DRIFT_THRESHOLD)
                        ]
                    },
                },
            ],
        ): Warn(),
    },
)


nudge(
    "Plan approved. Before implementing, use the TaskCreate tool to break the plan into "
    "tasks, then TaskUpdate them as you go. See: CLAUDE.md § Task Tracking.",
    only_if=[Tool("ExitPlanMode")],
    events=Event.PostToolUse,
    tests={
        Input(tool="ExitPlanMode"): Warn(),
        Input(tool="Edit", file="m.py"): Allow(),
    },
)


nudge(
    "This message has several distinct requests. Use the TaskCreate tool for each item "
    "before starting work, so none gets dropped. See: CLAUDE.md § Task Tracking.",
    skip_if=[InPlanMode()],
    events=Event.UserPromptSubmit,
    signals=Signals(
        [
            Signal(pattern=r"(^|\n)\s*[0-9]+[.)]\s", weight=2),
            Signal(pattern=r"(?s)(?:(?:^|\n)\s*[-*]\s).*?(?:(?:^|\n)\s*[-*]\s)", weight=2),
            Signal(
                pattern=r"\b(also|and also|additionally|another thing|one more thing|plus also)\b",
                weight=1,
                flags=re.I,
            ),
            Signal(pattern=rf"(?s)(?:.*?{IMPERATIVES}){{3}}", weight=2, flags=re.I),
            Signal(pattern=rf"(?s)(?:.*?{IMPERATIVES}){{2}}", weight=1, flags=re.I),
        ],
        threshold=2,
        window=1,
    ),
    tests={
        Input(prompt="1. add foo\n2. fix bar\n3. update baz"): Warn(),
        Input(prompt="just fix the typo"): Allow(),
        Input(prompt="1. add foo\n2. fix bar\n3. update baz", permission_mode="plan"): Allow(),
    },
)
