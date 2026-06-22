"""Condition evaluation: checks ``TCondition`` instances against the current event."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cc_transcript.models import OtherEvent
from cc_transcript.tools import tool_name_matches

from captain_hook.types import (
    Agent,
    Command,
    Content,
    CustomCondition,
    FilePath,
    InPlanMode,
    Or,
    Pattern,
    RanCommand,
    ReadFile,
    SourceEdits,
    TCondition,
    TestFile,
    Tool,
    TouchedFile,
    UsedSkill,
    Waiting,
)

if TYPE_CHECKING:
    from cc_transcript.activity import ToolUse
    from cc_transcript.query import Session

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookSpec


def has_completion_notification(t: Session, tool_use_id: str) -> bool:
    return any(
        isinstance(event, OtherEvent)
        and event.type == "queue-operation"
        and f"<tool-use-id>{tool_use_id}</tool-use-id>" in str(event.raw.get("content", ""))
        for event in t.events
    )


def waiting_tool_names(evt: BaseHookEvent) -> set[str]:
    from captain_hook.settings import DEFAULT_WAITING_TOOLS

    custom: object = getattr(evt.ctx.settings, "waiting_tools", None)
    return {str(x) for x in custom} if isinstance(custom, list) else set(DEFAULT_WAITING_TOOLS)


def ephemeral_wait(use: ToolUse, waiting_names: set[str]) -> bool:
    if use.call.name in waiting_names:
        return True
    match use.call.name:
        case "Agent" | "Task" | "Bash" if use.call.raw.get("run_in_background"):
            return True
        case "Agent" | "Task" if "subagent_type" not in use.call.raw:
            return True
    return False


def pending_async(use: ToolUse, t: Session) -> bool:
    match use.call.name:
        case "Agent" | "Task" if use.result and use.result.is_async:
            return not has_completion_notification(t, use.ref.tool_use_id)
        case "Workflow":
            return not has_completion_notification(t, use.ref.tool_use_id)
    return False


def is_waiting(evt: BaseHookEvent) -> bool:
    if not (t := evt.ctx.transcript):
        return False
    waiting_names = waiting_tool_names(evt)
    return any(ephemeral_wait(use, waiting_names) for use in t.current_turn.tool_calls) or any(
        pending_async(use, t) for use in t.tool_calls
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
            return bool(evt.tool_name) and tool_name_matches(evt.tool_name, pattern)
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
        case Pattern(pattern, lang, project_only):
            from captain_hook.ast_grep import lang_for_path
            from captain_hook.ast_grep import matches as ast_grep_matches

            if not (evt.content and (not project_only or is_project_file(evt))):
                return False
            resolved = lang or (lang_for_path(evt.file.path) if evt.file else None)
            return bool(resolved and ast_grep_matches(evt.content, resolved, pattern))
        case Agent(name):
            return bool(evt.agent_type) and evt.agent_type in name.split("|")
        case TestFile(project_only):
            return bool(evt.file and (not project_only or is_project_file(evt)) and evt.file.is_test)
        case SourceEdits(lang, include_tests, paths):
            return bool(
                evt.tool_name
                and tool_name_matches(evt.tool_name, "Edit|Write")
                and (f := evt.file)
                and f.matches(*SourceEdits(lang=lang).globs)
                and (include_tests or not f.is_test)
                and (paths is None or f.matches(paths))
            )
        case UsedSkill(name, subagents):
            return evt.ctx.transcript.has_skill(*name.split("|"), subagents=subagents)
        case ReadFile(patterns, subagents):
            return any(evt.ctx.transcript.has_read(p, subagents=subagents) for p in patterns)
        case TouchedFile(patterns, subagents):
            return evt.ctx.transcript.has_edit_to(*patterns, subagents=subagents)
        case RanCommand(pattern, subagents):
            return evt.ctx.transcript.has_command(pattern, subagents=subagents)
        case InPlanMode():
            return evt.permission_mode == "plan" or (
                (t := evt.ctx.transcript).tool_calls.named("EnterPlanMode").count()
                > t.tool_calls.named("ExitPlanMode").count()
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
