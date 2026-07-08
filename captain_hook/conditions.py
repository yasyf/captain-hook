"""Condition evaluation: checks ``TCondition`` instances against the current event."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.tools import SkillCall, WorkflowCall, tool_name_matches

from captain_hook.types import (
    Agent,
    And,
    Command,
    Content,
    CustomCondition,
    FilePath,
    FromSubagent,
    InPlanMode,
    McpTool,
    Not,
    Or,
    Pattern,
    RanCommand,
    ReadFile,
    ReadOnlyCommand,
    Runs,
    SkipPermissions,
    SourceEdits,
    TCondition,
    TestFile,
    Tool,
    ToolInput,
    TouchedFile,
    UsedSkill,
    Waiting,
    WorkflowScript,
)

if TYPE_CHECKING:
    from cc_transcript.activity import ToolUse
    from cc_transcript.notifications import Notifications
    from cc_transcript.query import Session

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookSpec


def waiting_tool_names(evt: BaseHookEvent) -> set[str]:
    from captain_hook.settings import DEFAULT_WAITING_TOOLS

    return set(settings.waiting_tools) if (settings := evt.ctx.settings) else set(DEFAULT_WAITING_TOOLS)


def ephemeral_wait(use: ToolUse, waiting_names: set[str]) -> bool:
    if use.call.name in waiting_names:
        return True
    match use.call.name:
        case "Agent" | "Task" | "Bash" if use.call.raw.get("run_in_background"):
            return True
        case "Agent" | "Task" if "subagent_type" not in use.call.raw:
            return True
    return False


def pending_async(use: ToolUse, notifications: Notifications) -> bool:
    match use.call.name:
        case "Agent" | "Task" if use.result and use.result.is_async:
            return not notifications.completed(use.ref.tool_use_id)
        case "Workflow" if not (use.result and use.result.is_error):
            return not notifications.completed(use.ref.tool_use_id)
    return False


def is_waiting(evt: BaseHookEvent) -> bool:
    if evt.background_tasks or evt.session_crons:
        return True
    if not (t := evt.ctx.transcript):
        return False
    notifications = t.notifications
    if notifications.has_pending:
        return True
    waiting_names = waiting_tool_names(evt)
    return any(ephemeral_wait(use, waiting_names) for use in t.current_turn.tool_calls) or any(
        pending_async(use, notifications) for use in t.tool_calls
    )


def workflow_script_source(evt: BaseHookEvent) -> str | None:
    """The pending ``Workflow`` call's script source, or ``None``.

    Returns the inline ``script`` when present, else reads the file at
    ``scriptPath``. A non-Workflow event, a missing/unreadable path, or a file
    over ~1 MiB yields ``None``; never raises.
    """
    if not (call := evt.as_input(WorkflowCall)):
        return None
    if call.script is not None:
        return call.script
    if not call.script_path:
        return None
    try:
        path = Path(call.script_path)
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        return path.read_text(errors="ignore")
    except OSError:
        return None


_WF_OPT_VALUE = r"""(?:'([^'\n]*)'|"([^"\n]*)"|`([^`\n]*)`|([$\w][\w.$]*))"""


def workflow_opt_matches(source: str, key: str) -> list[re.Match[str]]:
    return list(re.finditer(rf"\b{re.escape(key)}[ \t]*:[ \t]*{_WF_OPT_VALUE}", source))


def workflow_opt_values(source: str, key: str) -> list[str]:
    """Best-effort raw-source scan for values pinned to `key` in agent() opts. Never raises.

    Values are single-line: the whitespace around the colon is horizontal only, so a valueless
    ``key:`` at end of line does not swallow the next line's token.
    """
    return [next(g for g in m.groups() if g is not None) for m in workflow_opt_matches(source, key)]


def matches_workflow_script(cond: WorkflowScript, evt: BaseHookEvent) -> bool:
    if (source := workflow_script_source(evt)) is None:
        return False
    if cond.pattern is not None and not re.search(cond.pattern, source, re.MULTILINE):
        return False
    return all(
        any(re.search(pattern, value) for value in workflow_opt_values(source, key)) for key, pattern in cond.opts
    )


def coerce_tool_input(value: object) -> str | None:
    """Coerce a scalar raw tool-input value to matchable text; None for non-scalars."""
    match value:
        case str():
            return value
        case bool():
            return "true" if value else "false"
        case int() | float():
            return str(value)
        case _:
            return None


def has_read_glob(t: Session, *globs: str, subagents: bool = True) -> bool:
    from cc_transcript.query import sidechain_sessions

    return any(f.matches(*globs) for f in t.tool_calls.named("Read").files()) or (
        subagents and any(has_read_glob(sub, *globs) for sub in sidechain_sessions(t.path))
    )


def skill_name_matches(skill: str, names: tuple[str, ...]) -> bool:
    return skill in names or skill.split(":", 1)[-1] in names


def has_used_skill(t: Session, names: tuple[str, ...], *, subagents: bool = True) -> bool:
    from cc_transcript.query import sidechain_sessions

    return any(
        isinstance(call := use.call, SkillCall) and skill_name_matches(call.skill, names)
        for use in t.tool_calls.named("Skill")
    ) or (subagents and any(has_used_skill(sub, names) for sub in sidechain_sessions(t.path)))


def is_project_path(path: str | Path, root: Path | None) -> bool:
    """Whether ``path`` lies inside the repository ``root``.

    A relative path is always in-project; a ``None`` root (no repository detected) treats every
    path as in-project. An absolute path is in-project only when it resolves under ``root``.

    Args:
        path: A filesystem path as a ``str`` or ``Path`` — transcript ``FileRef`` paths are plain
            strings, hence the union.
        root: The repository root to test against, or ``None`` when unknown.
    """
    if not (p := Path(path)).is_absolute():
        return True
    if root is None:
        return True
    return p.resolve().is_relative_to(root.resolve())


def is_project_file(evt: BaseHookEvent) -> bool:
    return bool((file := evt.file) and is_project_path(file.path, evt.ctx.repo_root))


def check_condition(c: TCondition, evt: BaseHookEvent) -> bool:
    match c:
        case Tool(names):
            return bool(evt.tool_name) and tool_name_matches(evt.tool_name, "|".join(names))
        case FilePath(patterns, project_only):
            return bool(evt.file and (not project_only or is_project_file(evt)) and evt.file.matches(*patterns))
        case Command(pattern):
            return bool(
                (cl := evt.command_line) and any(re.search(pattern, s) for s in (cl.raw, *map(str, cl.commands)))
            )
        case Content(pattern, project_only):
            return bool(
                (not project_only or is_project_file(evt))
                and evt.content
                and re.search(pattern, evt.content, re.MULTILINE)
            )
        case ToolInput(fields):
            return bool(evt.tool_name) and all(
                (value := coerce_tool_input(evt.input.raw.get(field))) is not None
                and re.search(pattern, value, re.MULTILINE)
                for field, pattern in fields
            )
        case WorkflowScript() as workflow_script:
            return matches_workflow_script(workflow_script, evt)
        case Pattern(pattern, lang, project_only):
            from captain_hook.ast_grep import lang_for_path
            from captain_hook.ast_grep import matches as ast_grep_matches

            if not (evt.content and (not project_only or is_project_file(evt))):
                return False
            resolved = lang or (lang_for_path(evt.file.path) if evt.file else None)
            return bool(resolved and ast_grep_matches(evt.content, resolved, pattern))
        case Agent(names):
            return bool(evt.agent_type) and evt.agent_type in names
        case TestFile(project_only):
            return bool(evt.file and (not project_only or is_project_file(evt)) and evt.file.is_test)
        case SourceEdits(lang, include_tests, paths, project_only):
            return bool(
                evt.tool_name
                and tool_name_matches(evt.tool_name, "Edit|Write")
                and (f := evt.file)
                and (not project_only or is_project_file(evt))
                and f.matches(*SourceEdits(lang=lang).globs)
                and (include_tests or not f.is_test)
                and (not paths or f.matches(*paths))
            )
        case UsedSkill(names, subagents):
            return has_used_skill(evt.ctx.transcript, names, subagents=subagents)
        case ReadFile(patterns, subagents):
            return has_read_glob(evt.ctx.transcript, *patterns, subagents=subagents)
        case TouchedFile(patterns, subagents):
            return evt.ctx.transcript.has_edit_to(*patterns, subagents=subagents)
        case RanCommand(argv, subagents):
            return evt.ctx.transcript.has_command(*argv, subagents=subagents)
        case Runs(argv):
            return bool(argv and (cl := evt.command_line) and any(cmd.argv[: len(argv)] == argv for cmd in cl.commands))
        case ReadOnlyCommand():
            from captain_hook.readonly import is_read_only

            return bool((cl := evt.command_line) and is_read_only(cl))
        case McpTool():
            return bool(evt.tool_name) and evt.tool_name.startswith("mcp__")
        case InPlanMode():
            return evt.permission_mode == "plan" or (
                (t := evt.ctx.transcript).tool_calls.named("EnterPlanMode").count()
                > t.tool_calls.named("ExitPlanMode").count()
            )
        case Waiting():
            return is_waiting(evt)
        case FromSubagent():
            return evt.is_subagent
        case SkipPermissions():
            return evt.skip_permissions
        case And(conditions):
            return all(check_condition(sub, evt) for sub in conditions)
        case Not(condition):
            return not check_condition(condition, evt)
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
