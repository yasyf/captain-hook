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
    expand_tool_names,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookSpec


def check_condition(c: TCondition, evt: BaseHookEvent) -> bool:
    match c:
        case Tool(pattern):
            if not evt.tool_name:
                return False
            return evt.tool_name in expand_tool_names(pattern)
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
        case UsedSkill(name):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_skill(*name.split("|"))
        case ReadFile(patterns):
            return bool(evt.ctx.transcript) and any(evt.ctx.transcript.has_read(p) for p in patterns)
        case TouchedFile(patterns):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_edit_to(*patterns)
        case RanCommand(pattern):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_command(pattern)
        case InPlanMode():
            t = evt.ctx.transcript
            return bool(t) and t.count_tools("EnterPlanMode") > t.count_tools("ExitPlanMode")
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
