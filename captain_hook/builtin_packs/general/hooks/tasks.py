from __future__ import annotations

import re

from cc_transcript.filterspec import AGENT_INJECTION_GROUPS, TASK_NOTIFICATION_MARKER

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    FromSubagent,
    InPlanMode,
    Input,
    Signal,
    Signals,
    T,
    Tool,
    Waiting,
    Warn,
    hook,
    nudge,
)

OVERRIDE_TOKEN = "REMAINING_TASKS_ACKNOWLEDGED"
TASK_DRIFT_THRESHOLD = 8
DELIVERY_FRAME = re.compile(
    "|".join(
        (
            *(pattern for _, pattern in AGENT_INJECTION_GROUPS),
            rf"\A\s*{re.escape(TASK_NOTIFICATION_MARKER)}",
            r"\A\s*\[Subagent hand-back\]",
        )
    ),
    re.I,
)
LANE_ITEMS = (
    "It left three items:\n"
    "- An SQS grant row for the executor has to ship before the pop test can run.\n"
    "- The queue keeps messages for 4 days, so the 8 messages expire Mon ~11:20am PT.\n"
    "- A reply is drafted in aig/sns-poptest.md for when the owner grants the thread."
)
ORCA_DESK_REPORT = (
    '<agent-message from="orca-desk-9">\n'
    "aig-sns-poptest worker_done is verified (ctx_e57fc1e0de30 completed/succeeded).\n"
    f"{LANE_ITEMS}\n"
    "</agent-message>"
)

IMPERATIVES = (
    r"\b(?:add|fix|update|change|remove|create|implement|refactor|"
    r"move|rename|delete|replace|extract|split|merge|convert|migrate)\b"
)


class OpenTasks(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        return not evt.tasks.all_completed


class Overridden(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        return evt.ctx.t.has_override(OVERRIDE_TOKEN)


class IsDelivery(CustomCondition):
    """Matches a prompt the harness delivered from a teammate, another session, a task, or a subagent."""

    def check(self, evt: BaseHookEvent) -> bool:
        return bool(DELIVERY_FRAME.search(evt.user_prompt or ""))


class DriftedFromTasks(CustomCondition):
    """Matches when there are open tasks and many exploration calls since the last task touch."""

    def check(self, evt: BaseHookEvent) -> bool:
        if not evt.tasks.open:
            return False
        since = evt.ctx.t.after(tool="TaskCreate|TaskUpdate|TaskList|TaskGet")
        return since.tool_calls.named("Bash|Grep|Glob|WebSearch|WebFetch|LSP|Skill").count() >= TASK_DRIFT_THRESHOLD


hook(
    Event.Stop,
    f"Open tasks remain. Run `TaskUpdate` to complete each finished task, or output {OVERRIDE_TOKEN} to stop anyway.",
    only_if=[OpenTasks()],
    skip_if=[Waiting(), FromSubagent(), Overridden()],
    block=True,
    tests={
        Input(tasks=[{"id": "1", "subject": "a", "status": "completed"}]): Allow(),
        Input(tasks=[{"id": "1", "subject": "a", "status": "pending"}]): Block(),
        Input(
            tasks=[{"id": "1", "subject": "a", "status": "pending"}],
            transcript=[T.assistant(OVERRIDE_TOKEN)],
        ): Allow(),
    },
)


nudge(
    "Many calls have passed since the task list changed. Run `TaskUpdate` to record new work or a changed direction.",
    only_if=[Tool("Edit|Write"), DriftedFromTasks()],
    skip_if=[FromSubagent()],
    events=Event.PostToolUse,
    tests={
        Input(file="m.py", content="x = 1\n", tasks=[]): Allow(),
        Input(
            file="m.py",
            content="x = 1\n",
            tasks=[{"id": "1", "subject": "a", "status": "in_progress"}],
            transcript=[
                T.assistant(T.tool("TaskCreate")),
                T.assistant(*(T.tool("Bash", command="ls") for _ in range(TASK_DRIFT_THRESHOLD))),
            ],
        ): Warn(),
        Input(
            file="m.py",
            content="x = 1\n",
            agent_id="move-executor",
            tasks=[{"id": "1", "subject": "a", "status": "in_progress"}],
            transcript=[T.assistant(*(T.tool("Bash", command="ls") for _ in range(TASK_DRIFT_THRESHOLD)))],
        ): Allow(),
        Input(
            file="m.py",
            content="x = 1\n",
            tasks=[{"id": "1", "subject": "a", "status": "in_progress"}],
            transcript=[T.assistant(*(T.tool("Bash", command="ls") for _ in range(TASK_DRIFT_THRESHOLD)))],
        ): Allow(),
    },
)


nudge(
    "Plan approved. Run `TaskCreate` for each plan step before implementing.",
    only_if=[Tool("ExitPlanMode")],
    events=Event.PostToolUse,
    tests={
        Input(tool="ExitPlanMode"): Warn(),
        Input(tool="Edit", file="m.py"): Allow(),
    },
)


nudge(
    "This message has several distinct requests. Run `TaskCreate` for each before starting work.",
    skip_if=[InPlanMode(), IsDelivery()],
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
            Signal(pattern=rf"(?s){IMPERATIVES}(?:.*?{IMPERATIVES}){{2}}", weight=2, flags=re.I),
            Signal(pattern=rf"(?s){IMPERATIVES}.*?{IMPERATIVES}", weight=1, flags=re.I),
        ],
        threshold=2,
        window=0,
        origin="any",
    ),
    tests={
        Input(prompt="1. add foo\n2. fix bar\n3. update baz"): Warn(),
        Input(prompt="just fix the typo"): Allow(),
        Input(prompt="fix the parser, then add a test for it\n\n" + "the logs are attached below. " * 400): Allow(),
        Input(
            prompt="the logs are attached below. " * 400 + "\nfix the parser, add a test, and update the docs"
        ): Warn(),
        Input(prompt="1. add foo\n2. fix bar\n3. update baz", permission_mode="plan"): Allow(),
        Input(
            prompt="thanks, that works",
            transcript=[T.assistant("Done. I made three changes:\n1. Fixed X\n2. Added Y\n3. Updated Z")],
        ): Allow(),
        Input(
            prompt=(
                "<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n"
                "<summary>Agent finished</summary>\n<result>\n- `cc-transcript` -> version 7.0.1\n"
                "- `spawnllm` -> version 0.5.2\n</result>\n</task-notification>"
            )
        ): Allow(),
        Input(prompt=f"Another Claude session sent a message:\n{ORCA_DESK_REPORT}"): Allow(),
        Input(prompt=ORCA_DESK_REPORT): Allow(),
        Input(prompt=f'<teammate-message teammate_id="move-executor">\n{LANE_ITEMS}\n</teammate-message>'): Allow(),
        Input(prompt=f'<cross-session-message from="landing-desk">\n{LANE_ITEMS}\n</cross-session-message>'): Allow(),
        Input(prompt=f"[Subagent hand-back] The text below is a report.\n  {LANE_ITEMS}"): Allow(),
        Input(prompt=f"{LANE_ITEMS}\n\nAlso fix the parser and add a test."): Warn(),
        Input(
            prompt=(
                "1. add foo\n2. fix bar\n3. update baz\n\n"
                "Example log format:\n```text\n[Subagent hand-back] old report\n```"
            )
        ): Warn(),
        Input(prompt=f"Fix these:\n{ORCA_DESK_REPORT}"): Warn(),
        Input(prompt="the desk said <agent-message> again.\n1. add foo\n2. fix bar\n3. update baz"): Warn(),
    },
)
