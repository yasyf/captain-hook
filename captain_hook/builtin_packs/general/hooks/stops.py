from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, Input, LambdaCondition, on
from captain_hook.builtin_packs.general.hooks._sessions import clip

if TYPE_CHECKING:
    from collections.abc import Mapping

    from captain_hook import BaseHookEvent, HookResult, ToolRewriteEvent

STOP_TOOLS = frozenset({"TaskStop"})


def names_a_stop_tool(evt: BaseHookEvent) -> bool:
    return evt.tool_name in STOP_TOOLS


def describe_target(raw: Mapping[str, object]) -> str:
    match raw:
        case {"task_id": str() as task_id}:
            return f"task `{clip(task_id)}`"
        case {"shell_id": str() as shell_id}:
            return f"shell `{clip(shell_id)}`"
        case _:
            return "an unnamed target"


@on(
    Event.PreToolUse | Event.PermissionRequest,
    only_if=[LambdaCondition(names_a_stop_tool)],
    respect_gitignore=False,
    skip_planning_agents=False,
    mandatory=True,
    tests={
        Input(tool="TaskStop", tool_input={"task_id": "wcn64vfub"}): Block(
            pattern="`TaskStop` on task `wcn64vfub` cannot be verified"
        ),
        Input(tool="TaskStop", tool_input={"task_id": "wf_f79d45a5-908"}): Block(pattern="on task `wf_f79d45a5-908`"),
        Input(tool="TaskStop", tool_input={"task_id": "a7e6a10ac1f61999f"}): Block(
            pattern="which no session may stop, its own children included"
        ),
        Input(tool="TaskStop", tool_input={"task_id": "daemonkit-cache-impl"}): Block(
            pattern="Let it finish, or ask the owner to end it"
        ),
        Input(tool="TaskStop", tool_input={"shell_id": "bash_3"}): Block(
            pattern="on shell `bash_3` cannot be verified"
        ),
        Input(tool="TaskStop", tool_input={}): Block(pattern="on an unnamed target cannot be verified"),
        Input(tool="TaskStop", tool_input={"task_id": "wcn64vfub"}, agent_id="sub-1"): Block(
            pattern="cannot be verified"
        ),
        Input(tool="TaskStop", tool_input={"task_id": "wcn64vfub"}, permission_mode="plan"): Block(
            pattern="cannot be verified"
        ),
        Input(tool="TaskOutput", tool_input={"task_id": "wcn64vfub"}): Allow(),
        Input(tool="mcp__orca__TaskStop", tool_input={"task_id": "wcn64vfub"}): Allow(),
        Input(command="printf 'TaskStop wcn64vfub'"): Allow(),
    },
)
def stop_unverified_task(evt: ToolRewriteEvent) -> HookResult | None:
    return evt.block(
        f"BLOCKED: `{evt.tool_name}` on {describe_target(evt.input.raw)} cannot be verified: a bare id does not tell "
        "a disposable shell task from a workflow, agent, or teammate session, which no session may stop, its own "
        "children included. Let it finish, or ask the owner to end it."
    )
