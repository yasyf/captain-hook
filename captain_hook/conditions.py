"""Condition evaluation: checks ``TCondition`` instances against the current event."""

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
    Or,
    RanCommand,
    ReadFile,
    SourceEdits,
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
    return any((n := m.notification) and n.tool_use_id == tool_use_id for m in t.messages[after_idx + 1 :])


def waiting_tool_names(evt: BaseHookEvent) -> set[str]:
    from captain_hook.settings import DEFAULT_WAITING_TOOLS

    custom: object = getattr(evt.ctx.settings, "waiting_tools", None)
    return {str(x) for x in custom} if isinstance(custom, list) else set(DEFAULT_WAITING_TOOLS)


def ephemeral_wait(tu: ToolUse, waiting_names: set[str]) -> bool:
    if tu.name in waiting_names:
        return True
    match tu.name:
        case "Agent" | "Task" | "Bash" if tu.raw_input.get("run_in_background"):
            return True
        case "Agent" | "Task" if "subagent_type" not in tu.raw_input:
            return True
    return False


def pending_async(tu: ToolUse, t: Transcript) -> bool:
    match tu.name:
        case "Agent" | "Task" if tu.result and tu.result.is_async:
            return not has_completion_notification(t, tu.id, tu.message_index)
        case "Workflow":
            return not has_completion_notification(t, tu.id, tu.message_index)
    return False


def is_waiting(evt: BaseHookEvent) -> bool:
    if not (t := evt.ctx.transcript):
        return False
    waiting_names = waiting_tool_names(evt)
    return any(ephemeral_wait(tu, waiting_names) for tu in t.current_turn.tool_uses) or any(
        pending_async(tu, t) for tu in t.tool_uses
    )


def is_project_file(evt: BaseHookEvent) -> bool:
    if not (file := evt.file):
        return False
    if not file.path.is_absolute():
        return True
    if not (root := evt.ctx.repo_root):
        return True
    return file.path.resolve().is_relative_to(root.resolve())


def check_condition(c: TCondition, evt: BaseHookEvent) -> bool:
    match c:
        case Tool(pattern):
            if not evt.tool_name:
                return False
            return any(tool_name_matches(evt.tool_name, p) for p in pattern.split("|"))
        case FilePath(patterns, project_only):
            return bool(evt.file and (not project_only or is_project_file(evt)) and evt.file.matches(*patterns))
        case Command(pattern):
            return bool((cl := evt.command_line) and any(re.search(pattern, str(cmd)) for cmd in cl.commands))
        case Content(pattern, project_only):
            return bool(
                (not project_only or is_project_file(evt))
                and evt.content
                and re.search(pattern, evt.content, re.MULTILINE)
            )
        case Agent(name):
            return bool(evt.agent_type) and evt.agent_type in name.split("|")
        case TestFile(project_only):
            return bool(evt.file and (not project_only or is_project_file(evt)) and evt.file.is_test)
        case SourceEdits(lang, include_tests, paths):
            return bool(
                evt.tool_name
                and any(tool_name_matches(evt.tool_name, n) for n in ("Edit", "Write"))
                and (f := evt.file)
                and f.matches(*SourceEdits(lang=lang).globs)
                and (include_tests or not f.is_test)
                and (paths is None or f.matches(paths))
            )
        case UsedSkill(name, subagents):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_skill(*name.split("|"), subagents=subagents)
        case ReadFile(patterns, subagents):
            return bool(evt.ctx.transcript) and any(
                evt.ctx.transcript.has_read(p, subagents=subagents) for p in patterns
            )
        case TouchedFile(patterns, subagents):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_edit_to(*patterns, subagents=subagents)
        case RanCommand(pattern, subagents):
            return bool(evt.ctx.transcript) and evt.ctx.transcript.has_command(pattern, subagents=subagents)
        case InPlanMode():
            return evt.permission_mode == "plan" or (
                bool(t := evt.ctx.transcript) and t.count_tools("EnterPlanMode") > t.count_tools("ExitPlanMode")
            )
        case Waiting():
            return is_waiting(evt)
        case Or(conditions):
            return any(check_condition(sub, evt) for sub in conditions)
        case CustomCondition():
            return c.check(evt)
    return False


def matches_conditions(spec: HookSpec, evt: BaseHookEvent) -> bool:
    if any(not check_condition(c, evt) for c in spec.only_if):
        return False
    if any(check_condition(c, evt) for c in spec.skip_if):
        return False
    return True
