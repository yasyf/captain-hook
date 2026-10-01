from __future__ import annotations

import re
from dataclasses import dataclass

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Clause,
    CustomCondition,
    Event,
    FilePath,
    FromSubagent,
    InPlanMode,
    Input,
    Phrase,
    RewritingExistingPlan,
    T,
    Tool,
    UsedTool,
    hook,
)
from captain_hook.signals.nlp import dep_related, find_lemma_matches, parse, scan_text, verb_candidates

ENTER_PLAN_MODE = Clause(
    noun=Phrase("mode", "planning"),
    verb=Phrase("enter", "re-enter", "reenter", "return", "go", "switch", "get", "come"),
    subject=("unnamed",),
)
STOP_WORK = (
    Clause(noun=Phrase("work"), verb=Phrase("do"), negated=True),
    Clause(noun=Phrase("work"), verb=Phrase("stop", "halt", "pause")),
)
ORCA_WORKER_BRIEF = (
    "Please carry out this task from my Orca coordinator by following the brief I pasted below.\n\n"
    '<pasted_content id="p1">\n'
    "You are working inside Orca, a multi-agent IDE. You are a dispatched worker.\n\n"
    "=== AFTER YOU SEND worker_done ===\n\n"
    "worker_done ends your turn for this task. Your dispatched work is complete:\n"
    "stop, return to an idle prompt, and take no further actions — do NOT start\n"
    "new or unrelated work, do NOT run a sleep/poll loop, and do NOT keep calling\n"
    "`orca orchestration check`.\n\n"
    "=== TASK ===\n"
    "Lane plan-text: read /x/plan.md in full first and execute it exactly. "
    "No lane stops for a design question: it picks its recommended option and keeps going. "
    "Never end a turn waiting and never park; the plan wins.\n"
    '</pasted_content id="p1">\n'
)


def directs_plan_mode(text: str) -> bool:
    return bool(text.strip()) and any(
        not any(c.dep_ == "neg" for c in verb.children)
        and any(dep_related(noun, verb) for noun in find_lemma_matches(ENTER_PLAN_MODE.noun, sent, {"NOUN", "PROPN"}))
        for sent in parse(text).sents
        for verb in verb_candidates(ENTER_PLAN_MODE, sent)
    )


def directs_replanning(prompt: str) -> bool:
    typed = re.sub(r"<pasted_content\b[^>]*>.*?</pasted_content\b[^>]*>", " ", prompt, flags=re.DOTALL)
    return directs_plan_mode(typed) or (
        scan_text(typed, STOP_WORK) and scan_text(typed, (r"(?<![\w./-])(?:re-)?plan",))
    )


class DirectsReplanning(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        return directs_replanning(evt.ctx.turn.user_text)


@dataclass(frozen=True, slots=True)
class ToolDisallowed(CustomCondition):
    name: str

    def check(self, evt: BaseHookEvent) -> bool:
        return self.name in evt.disallowed_tools


hook(
    Event.PreToolUse,
    only_if=[Tool("Write"), RewritingExistingPlan()],
    message=(
        "This plan file was already written in this planning session. Use the Edit tool "
        "to make incremental changes instead of rewriting the entire plan with Write."
    ),
    block=True,
    tests={
        Input(
            tool="Write",
            file="/x/plans/p.md",
            content="# Plan v2",
            transcript=[
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v1")),
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v2")),
            ],
        ): Block(),
        Input(
            tool="Write",
            file="/x/plans/p.md",
            content="# Plan v2",
            transcript=[
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v1")),
                T.assistant(T.tool("EnterPlanMode")),
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v2")),
            ],
        ): Allow(),
        Input(tool="Write", file="/x/plans/p.md", content="# Plan", transcript=[]): Allow(),
        Input(tool="Write", file="/x/src/main.py", content="x = 1"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[Tool.EditTools, DirectsReplanning()],
    skip_if=[
        FromSubagent(),
        InPlanMode(),
        UsedTool("ExitPlanMode"),
        ToolDisallowed("ExitPlanMode"),
        FilePath("**/plans/*.md", project_only=False),
    ],
    message=(
        "Plan mode was requested, so no more edits until the user approves a plan. "
        "Call `ExitPlanMode` with the plan, entering plan mode first if needed."
    ),
    block=True,
    tests={
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("Re-enter plan mode, don't do any more work.")],
        ): Block(pattern="plan mode"),
        Input(
            tool="Write",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("Stop all work until we agree on a plan.")],
        ): Block(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[
                T.user("Re-enter plan mode, don't do any more work."),
                T.assistant(T.tool("EnterPlanMode")),
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("please fix the typo in main.py")],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("go back to plan mode")],
        ): Block(pattern="plan mode"),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[
                T.user("The retry loop and the timeout are both wrong. Re-enter plan mode, don't do any more work.")
            ],
        ): Block(pattern="plan mode"),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("don't go back to plan mode, just fix it")],
        ): Allow(),
        Input(
            tool="Write",
            file="/x/.claude/plans/p.md",
            content="# Plan",
            transcript=[
                T.user(
                    "update the plan again so we can compact again (dont enter plan mode), dump all context that "
                    "would be needed on restart, and lets be more cautious about our main agent context moving forward"
                )
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/Users/me/.claude/plans/p.md",
            content="# Plan",
            permission_mode="bypassPermissions",
            transcript=[
                T.user(
                    "update the plan again so we can compact again (dont enter"
                    + " " * 112
                    + "\n              plan mode), dump all context that would be needed on restart. also fix the hook "
                    "that blocked you from doing this in a background agent"
                )
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/Users/me/.claude/plans/p.md",
            content="# Plan",
            transcript=[T.user("Stop all work until we agree on a plan. Put the new plan in the plan file.")],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("stop, go back to plan mode")],
        ): Block(pattern="plan mode"),
        Input(
            tool="Write",
            file="/x/.claude/plans/p.md",
            content="# Plan",
            permission_mode="plan",
            transcript=[T.user("Stop all work until we agree on a plan.")],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            permission_mode="acceptEdits",
            transcript=[
                T.user("The retry loop and the timeout are both wrong. Re-enter plan mode, don't do any more work."),
                T.assistant(T.tool("ExitPlanMode", plan="# Plan")),
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            permission_mode="acceptEdits",
            transcript=[
                T.user("Re-enter plan mode, don't do any more work."),
                T.assistant(T.tool("EnterPlanMode")),
                T.assistant(T.tool("ExitPlanMode", plan="# Plan")),
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/upload.py",
            content="x = 1",
            transcript=[T.user("stop the work on the uploader, just let it fail")],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            permission_mode="acceptEdits",
            transcript=[
                T.user("add retries to the uploader"),
                T.assistant(T.tool("EnterPlanMode")),
                T.assistant(T.tool("ExitPlanMode", plan="# Plan")),
                T.user("Stop all work until we agree on a plan."),
            ],
        ): Block(),
        Input(
            tool="Write",
            file="/x/LANE-REPORT.md",
            content="# report",
            agent_id="tm1",
            transcript=[
                T.user(
                    "STOP and report rather than guessing. If a write is refused with "
                    "'Plan mode was requested', that is the hook you are fixing."
                )
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("In a workflow, verification agents never outnumber the agents doing the work.")],
        ): Allow(),
        Input(
            tool="Write",
            file="/x/go/ci/internal/release/pipeline/plantext.go",
            content="package pipeline",
            permission_mode="bypassPermissions",
            transcript=[T.user(ORCA_WORKER_BRIEF)],
        ): Allow(),
        Input(
            tool="Write",
            file="/x/go/ci/internal/release/pipeline/plantext.go",
            content="package pipeline",
            transcript=[T.user("Stop all work until we agree on a plan.\n\n" + ORCA_WORKER_BRIEF)],
        ): Block(pattern="plan mode"),
        Input(
            tool="Edit",
            file="/x/src/upload.py",
            content="x = 1",
            transcript=[T.user("Stop the work on the uploader and push user/plan-text-base.")],
        ): Allow(),
        Input(
            tool="Write",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("Stop all work. We need to re-plan the approach.")],
        ): Block(pattern="plan mode"),
        Input(
            tool="Write",
            file="/x/src/main.py",
            content="x = 1",
            permission_mode="bypassPermissions",
            disallowed_tools=("AskUserQuestion", "EnterPlanMode", "ExitPlanMode"),
            transcript=[T.user("Stop all work until we agree on a plan.")],
        ): Allow(),
        Input(
            tool="Write",
            file="/x/src/main.py",
            content="x = 1",
            permission_mode="bypassPermissions",
            transcript=[T.user("Stop all work until we agree on a plan.")],
        ): Block(pattern="plan mode"),
    },
)
