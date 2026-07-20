"""Condition evaluation: checks ``TCondition`` instances against the current event."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.tools import SkillCall, TaskCall, WorkflowCall, tool_name_matches

from captain_hook.types import (
    Agent,
    And,
    Command,
    Content,
    CustomCommandLineCondition,
    CustomCondition,
    FilePath,
    FromSubagent,
    InPlanMode,
    Not,
    Or,
    Pattern,
    RanCommand,
    ReadFile,
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
from captain_hook.util import reqenv
from captain_hook.util.scratch import is_scratch_path

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine
    from cc_transcript.query import Session

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookSpec

# Prose and config file extensions that shouldn't, on their own, demand a code-review pass.
NON_SOURCE_SUFFIXES = (
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".lock",
)


def waiting_tool_names(evt: BaseHookEvent) -> frozenset[str]:
    from captain_hook.settings import DEFAULT_WAITING_TOOLS

    return frozenset(settings.waiting_tools if (settings := evt.ctx.settings) else DEFAULT_WAITING_TOOLS)


def is_waiting(evt: BaseHookEvent) -> bool:
    from cc_transcript.activity_probe import session_activity_probe

    if evt.background_tasks or evt.session_crons:
        return True
    if not (t := evt.ctx.transcript) or t.path is None:
        return False
    return session_activity_probe(t.path, waiting_tools=waiting_tool_names(evt)).is_waiting


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


class UserSaid(CustomCondition):
    """Matches when the user's messages contain any of the given keywords."""

    def __init__(self, *keywords: str) -> None:
        self.keywords = keywords

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.ctx.t.user_said(*self.keywords)


class AllEditsUnder(CustomCondition):
    """Matches when every edit this session is under one of the given path prefixes."""

    def __init__(self, *prefixes: str) -> None:
        self.prefixes = prefixes

    def check(self, evt: BaseHookEvent) -> bool:
        return bool(files := evt.ctx.t.edited_files) and all(f.under(*self.prefixes) for f in files)


class FromTeammate(CustomCondition):
    """Matches when the current turn's in-flight subagent spawn is a named teammate.

    A teammate spawn carries a ``name`` in its Agent/Task input, which cc-transcript surfaces as
    ``TaskCall.agent_name``; a plain subagent leaves it unset. Scoped to the current turn so a
    teammate still running from an earlier turn never re-fires against a later unnamed spawn.

    Example:
        >>> nudge("Return tight digests", only_if=[FromTeammate()], events=Event.SubagentStart)
    """

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            isinstance(use.call, TaskCall) and bool(use.call.agent_name)
            for use in evt.ctx.t.current_turn.tool_calls.named("Task")
            if use.result is None
        )


class FreshSession(CustomCondition):
    """Matches a ``SessionStart`` from a fresh ``startup`` or ``clear``, not a ``resume`` or ``compact``.

    Example:
        >>> nudge("Preload your tools", only_if=[FreshSession()], events=Event.SessionStart)
    """

    def check(self, evt: BaseHookEvent) -> bool:
        from captain_hook.events import SessionStartEvent

        return isinstance(evt, SessionStartEvent) and evt.source in ("startup", "clear")


class Headless(CustomCondition):
    """Matches a headless ``claude -p`` / SDK run (``CLAUDE_CODE_ENTRYPOINT`` in the ``sdk-*`` family).

    Example:
        >>> llm_gate("Check docs freshness", skip_if=[Headless()], events=Event.Stop)
    """

    def check(self, evt: BaseHookEvent) -> bool:
        return reqenv.is_headless()


class RewritingExistingPlan(CustomCondition):
    """Matches a ``Write`` to a plan file (``.md`` under ``plans/`` or ``specs/``) already written this
    session, with no new plan cycle (``EnterPlanMode``) since the last ``Write`` to it.

    Reads from ``evt.ctx.prior`` (the window before the current turn's last exchange) so the pending
    ``Write`` being evaluated is never itself counted as the prior edit. A write to the file this
    session already implies it exists, so no filesystem check is needed.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Tool("Write"), RewritingExistingPlan()],
        ...      message="Edit the plan instead of rewriting it", block=True)
    """

    def check(self, evt: BaseHookEvent) -> bool:
        fp = evt.file
        if not fp or fp.suffix != ".md" or not fp.under("plans/", "specs/"):
            return False
        if not evt.ctx.prior.has_edit_to(str(fp)):
            return False
        return not evt.ctx.prior.after(tool="Write", file=str(fp)).has_tool("EnterPlanMode")


class Commits(CustomCommandLineCondition):
    """Matches a command line whose primary command explicitly names a path ending in ``suffix``.

    Pairs with a ``git commit`` filter to gate a language's test run only when the commit stages a
    file of that language — ``Commits(".py")``, ``Commits(".go")``.

    Example:
        >>> gate("Run pytest first", only_if=[Runs("git", "commit"), Commits(".py")])
    """

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return (cmd := cl.primary) is not None and self.suffix in str(cmd)


class ScratchPath(CustomCondition):
    """Matches a file-tool target resolving into a system temp root or a scratch-named directory.

    Resolves a relative path against ``evt.cwd`` (a relative path with no cwd never matches), then
    fully resolves it — following a final-component symlink — before the scratch-path heuristic. The
    full resolve is a security boundary: this gates the skip-permissions auto-approver, so a symlink
    that sits in a scratch directory but points at a real file must not be treated as scratch.

    Example:
        >>> approve("scratch writes", only_if=[Tool("Write"), ScratchPath(), SkipPermissions()])
    """

    def check(self, evt: BaseHookEvent) -> bool:
        if (file := evt.file) is None:
            return False
        if not (path := Path(file.path)).is_absolute():
            if evt.cwd is None:
                return False
            path = evt.cwd / path
        return is_scratch_path(path.resolve())


class EditedSource(CustomCondition):
    """Matches when the session edited a non-test, in-repo source file (docs and config excluded).

    ``exclude_suffixes`` tailors which extensions count as non-source (prose/config); it defaults to
    :data:`NON_SOURCE_SUFFIXES`.

    Example:
        >>> llm_gate("Review the diff before stopping", only_if=[EditedSource()], events=Event.Stop)
    """

    def __init__(self, *, exclude_suffixes: Sequence[str] = NON_SOURCE_SUFFIXES) -> None:
        self.exclude_suffixes = tuple(exclude_suffixes)

    def check(self, evt: BaseHookEvent) -> bool:
        root = evt.ctx.repo_root
        return any(
            not f.is_test
            and f.suffix not in self.exclude_suffixes
            and not f.under("docs", ".claude", ".github")
            and is_project_path(f.path, root)
            for f in evt.ctx.t.tool_calls.named("Edit|Write").files()
        )


class Redirects(CustomCommandLineCondition):
    """Matches when any command in the line carries a shell redirect (input or output).

    Detects file redirects on the parsed commands — ``>``, ``>>``, ``2>``, ``<``, and fd
    duplications such as ``2>&1``. A bare pipe (``echo x | cat``) moves data between commands but
    is not a redirect, so it never matches.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Tool("Bash"), Redirects()], message="no redirects", block=True)
    """

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(cmd.redirects for cmd in cl.commands)


def check_condition(c: TCondition, evt: BaseHookEvent) -> bool:
    match c:
        case Tool(names):
            return bool(evt.tool_name) and tool_name_matches(evt.tool_name, "|".join(names))
        case FilePath(patterns, project_only):
            return bool(evt.file and (not project_only or is_project_file(evt)) and evt.file.matches(*patterns))
        case Command(pattern):
            return bool(
                (cmd := evt.cmd).raw and any(re.search(pattern, s) for s in (cmd.raw, *map(str, cmd.line.commands)))
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
            return bool(argv and (cmd := evt.cmd).raw and any(c.argv[: len(argv)] == argv for c in cmd.line.commands))
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
