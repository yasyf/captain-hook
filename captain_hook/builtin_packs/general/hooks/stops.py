from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, Input, LambdaCondition, on
from captain_hook.builtin_packs.general.hooks._sessions import (
    INLINE_COMMANDS,
    INLINE_STARTED,
    INLINE_TRANSCRIPT,
    LATER_SESSION,
    TASK_STOP,
    block_first,
    clip,
    inline_ruling,
    inline_spawn,
    lift,
    stand_down_count,
)
from captain_hook.grants import Proposal

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent, HookResult, ToolRewriteEvent

STOP_TOOLS = frozenset({"TaskStop"})
OWN_LANE = "lane-2@session-67c0e5da"
stopping = partial(Input, tool="TaskStop", commands=INLINE_COMMANDS, transcript=INLINE_TRANSCRIPT)


def names_a_stop_tool(evt: BaseHookEvent) -> bool:
    return evt.tool_name in STOP_TOOLS


def stop_target(raw: object) -> tuple[str, str] | None:
    match raw:
        case {"task_id": str() as task_id}:
            return "task", task_id
        case {"shell_id": str() as shell_id}:
            return "shell", shell_id
        case _:
            return None


def describe_target(target: tuple[str, str] | None) -> str:
    return "an unnamed target" if target is None else f"{target[0]} `{clip(target[1])}`"


@on(
    Event.PreToolUse | Event.PermissionRequest,
    only_if=[LambdaCondition(names_a_stop_tool)],
    respect_gitignore=False,
    skip_planning_agents=False,
    mandatory=True,
    tests={
        stopping(tool_input={"task_id": "wcn64vfub"}): Block(
            pattern="`TaskStop` on task `wcn64vfub` cannot be verified"
        ),
        stopping(tool_input={"task_id": "wf_f79d45a5-908"}): Block(pattern="on task `wf_f79d45a5-908`"),
        stopping(tool_input={"task_id": "a7e6a10ac1f61999f"}): Block(pattern="its own children included"),
        stopping(tool_input={"task_id": "daemonkit-cache-impl"}): Block(
            pattern=f"Let it finish, or ask the owner to end it or {LATER_SESSION}"
        ),
        stopping(tool_input={"shell_id": "bash_3"}): Block(pattern="on shell `bash_3` cannot be verified"),
        stopping(tool_input={}): Block(pattern="on an unnamed target cannot be verified"),
        stopping(tool_input={"task_id": "wcn64vfub"}, agent_id="sub-1"): Block(pattern="cannot be verified"),
        stopping(tool_input={"task_id": "wcn64vfub"}, permission_mode="plan"): Block(pattern="cannot be verified"),
        stopping(
            tool_input={"task_id": "wcn64vfub"},
            commands={**INLINE_COMMANDS, "ccn answer search wcn64vfub": inline_ruling("Stop wcn64vfub, it hung.")},
        ): Allow(),
        stopping(
            tool_input={"task_id": "wcn64vfub"},
            commands={
                **INLINE_COMMANDS,
                "ccn answer search wcn64vfub": inline_ruling(
                    "Stop wcn64vfub.", written=INLINE_STARTED + timedelta(seconds=1)
                ),
            },
        ): Block(pattern="cannot be verified"),
        stopping(
            tool_input={"task_id": "wcn64vfub"},
            commands={**INLINE_COMMANDS, "ccn answer search wcn64vfub": inline_ruling("Stop wcn64vfub2.")},
        ): Block(pattern="cannot be verified"),
        stopping(tool_input={"task_id": OWN_LANE}, transcript=inline_spawn(OWN_LANE)): Allow(),
        stopping(tool_input={"task_id": OWN_LANE}, transcript=inline_spawn(OWN_LANE, tool="Task")): Allow(),
        stopping(tool_input={"task_id": OWN_LANE}, agent_id="lane-1", transcript=inline_spawn(OWN_LANE)): Allow(),
        stopping(tool_input={"task_id": OWN_LANE}): Block(pattern=f"on task `{OWN_LANE}` cannot be verified"),
        stopping(tool_input={"task_id": OWN_LANE}, transcript=inline_spawn("lane-3@session-67c0e5da")): Block(
            pattern="cannot be verified"
        ),
        stopping(tool_input={"task_id": OWN_LANE}, transcript=inline_spawn(OWN_LANE, status="async_launched")): Block(
            pattern="cannot be verified"
        ),
        stopping(tool_input={"task_id": OWN_LANE}, transcript=inline_spawn(OWN_LANE, tool="Bash")): Block(
            pattern="cannot be verified"
        ),
        stopping(tool_input={"task_id": OWN_LANE}, agent_id="lane-1", root_transcript=inline_spawn(OWN_LANE)): Block(
            pattern="cannot be verified"
        ),
        stopping(tool_input={"task_id": "aig-no-delete-plan@session-756e25cc"}): Block(
            pattern="on task `aig-no-delete-plan@session-756e25cc` cannot be verified"
        ),
        stopping(tool_input={"task_id": "lane-2"}, transcript=inline_spawn("lane-2")): Block(
            pattern="on task `lane-2` cannot be verified"
        ),
        Input(tool="TaskOutput", tool_input={"task_id": "wcn64vfub"}): Allow(),
        Input(tool="mcp__orca__TaskStop", tool_input={"task_id": "wcn64vfub"}): Allow(),
        Input(command="printf 'TaskStop wcn64vfub'"): Allow(),
    },
)
def stop_unverified_task(evt: ToolRewriteEvent) -> HookResult | None:
    target = stop_target(evt.input.raw)
    message = (
        f"BLOCKED: `{evt.tool_name}` on {describe_target(target)} "
        "cannot be verified as a disposable shell task rather than a workflow, agent, or teammate session, its own "
        f"children included. Let it finish, or ask the owner to end it or {LATER_SESSION}."
    )
    if target is None:
        return evt.block(message)
    message += stand_down_count(target[1])
    action = Proposal(scope={"task": target[1]}, summary=f"stop {target[0]} {target[1]}")
    return block_first(evt, (lift(evt, TASK_STOP, action, message),))
