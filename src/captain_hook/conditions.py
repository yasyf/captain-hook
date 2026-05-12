from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook.types import (
    Agent,
    Command,
    Content,
    CustomCondition,
    FilePath,
    InPlanMode,
    RanCommand,
    ReadFile,
    TCondition,
    TestFile,
    Tool,
    TouchedFile,
    UsedSkill,
    Waiting,
    tool_name_matches,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.transcript import Transcript
    from captain_hook.transcript.models import ToolUse
    from captain_hook.types import HookSpec


def has_completion_notification(t: Transcript, tool_use_id: str, after_idx: int) -> bool:
    return any(
        (n := m.notification) and n.tool_use_id == tool_use_id
        for m in t.messages[after_idx + 1 :]
    )


def tool_use_waiting(tu: ToolUse, t: Transcript) -> bool:
    match tu.name:
        case "Monitor" | "TeamCreate" | "ScheduleWakeup" | "SendMessage":
            return True
        case "Agent" | "Task" | "Bash" if tu.raw_input.get("run_in_background"):
            return True
        case "Agent" | "Task" if "subagent_type" not in tu.raw_input:
            return True
        case "Agent" | "Task" if tu.result and tu.result.is_async:
            return not has_completion_notification(t, tu.id or "", tu.message_index)
    return False


def is_waiting(evt: BaseHookEvent) -> bool:
    if not (t := evt.ctx.transcript):
        return False
    turn = t.current_turn
    return any(tool_use_waiting(tu, turn) for tu in turn.tool_uses)


def check_condition(c: TCondition, evt: BaseHookEvent) -> bool:
    match c:
        case Tool(pattern):
            if not evt.tool_name:
                return False
            return any(tool_name_matches(evt.tool_name, p) for p in pattern.split("|"))
        case FilePath(patterns):
            return bool(evt.file and evt.file.matches(*patterns))
        case Command(pattern):
            return bool((cl := evt.command_line) and any(re.search(pattern, str(cmd)) for cmd in cl.commands))
        case Content(pattern):
            return bool(evt.content and re.search(pattern, evt.content, re.MULTILINE))
        case Agent(name):
            return bool(evt.agent_type and evt.agent_type in name.split("|"))
        case TestFile():
            return bool(evt.file and evt.file.is_test)
        case UsedSkill(name, subagents):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_skill(*name.split("|"), subagents=subagents)
        case ReadFile(patterns, subagents):
            return bool(evt.ctx.transcript) and any(evt.ctx.transcript.has_read(p, subagents=subagents) for p in patterns)
        case TouchedFile(patterns, subagents):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_edit_to(*patterns, subagents=subagents)
        case RanCommand(pattern, subagents):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_command(pattern, subagents=subagents)
        case InPlanMode():
            return evt.permission_mode == "plan" or (
                bool(t := evt.ctx.transcript)
                and t.count_tools("EnterPlanMode") > t.count_tools("ExitPlanMode")
            )
        case Waiting():
            return is_waiting(evt)
        case CustomCondition():
            return c.check(evt)
        case _:
            return False


def matches_conditions(spec: HookSpec, evt: BaseHookEvent) -> bool:
    if any(not check_condition(c, evt) for c in spec.only_if):
        return False
    if any(check_condition(c, evt) for c in spec.skip_if):
        return False
    return True
