from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_transcript.command import Command, CommandLine, Redirect
    from cc_transcript.tools import (
        BashCall,
        EditCall,
        ExitPlanModeCall,
        GlobCall,
        GrepCall,
        MultiEditCall,
        NotebookEditCall,
        OtherCall,
        ReadCall,
        SkillCall,
        TaskCall,
        TaskCreateCall,
        TaskUpdateCall,
        ToolCall,
        ToolCallBase,
        WriteCall,
    )

    from captain_hook import file, style, util
    from captain_hook.app import hook, on
    from captain_hook.ast_grep import COMMENT_TYPES
    from captain_hook.conditions import workflow_opt_matches, workflow_opt_values, workflow_script_source
    from captain_hook.context import HookContext
    from captain_hook.contexts import (
        AfterEdit,
        BeforeEdit,
        Excerpts,
        Introduced,
        PromptContext,
        WorkflowScriptSource,
        apply_contexts,
        excerpt_around,
    )
    from captain_hook.durable import DurableSlot, DurableState, DurableStore
    from captain_hook.events import (
        BackgroundTask,
        BaseHookEvent,
        NotificationEvent,
        PermissionRequestEvent,
        PostToolUseEvent,
        PostToolUseFailureEvent,
        PreCompactEvent,
        PreToolUseEvent,
        SessionCron,
        SessionEndEvent,
        SessionStartEvent,
        StopEvent,
        SubagentStartEvent,
        SubagentStopEvent,
        ToolHookEvent,
        ToolRewriteEvent,
        UserPromptSubmitEvent,
    )
    from captain_hook.fields import Deque
    from captain_hook.file import File, categorize_files
    from captain_hook.primitives import (
        GateVerdict,
        NudgeVerdict,
        PromptCheckVerdict,
        SafetyVerdict,
        approve,
        block_command,
        deny,
        gate,
        llm_approve,
        llm_evaluate,
        llm_gate,
        llm_nudge,
        prompt_check,
        rewrite_code,
        rewrite_command,
        set_tool_input,
        warn_command,
    )

    # lint/nudge are imported from their defining modules, not the primitives
    # package: the package attribute and the submodule share a name, and an alias
    # targeting captain_hook.primitives.<name> resolves to the module under static
    # analysis (griffe), shadowing the function.
    from captain_hook.primitives.lint import diff_lint, lint
    from captain_hook.primitives.nudge import nudge
    from captain_hook.primitives.workflow import Artifact, Step, Workflow, text_matches, workflow
    from captain_hook.prompt import Prompt
    from captain_hook.session import SessionSlot, SessionStore, session_state
    from captain_hook.settings import HooksSettings, build_settings
    from captain_hook.signals.nlp import Clause, NlpSignal, Phrase, has_nominal_subject, is_past_predicate
    from captain_hook.state import HookState, PrimitiveState, WorkflowState, workflow_state
    from captain_hook.tasks import Task, Tasks
    from captain_hook.testing import (
        Allow,
        Ask,
        Block,
        FileFixture,
        InlineTests,
        Input,
        Rewrite,
        TranscriptFixture,
        Warn,
    )
    from captain_hook.types import (
        Action,
        Agent,
        And,
        Content,
        CustomCommandLineCondition,
        CustomCondition,
        CustomInputTypeCondition,
        Event,
        FilePath,
        FromSubagent,
        HookResponse,
        HookResult,
        InPlanMode,
        Not,
        Or,
        Pattern,
        RanCommand,
        ReadFile,
        Runs,
        Signal,
        Signals,
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
    from captain_hook.util import read_json, resolve_binary

_EXPORTS: dict[str, str] = {
    "hook": "captain_hook.app",
    "on": "captain_hook.app",
    "COMMENT_TYPES": "captain_hook.ast_grep",
    "workflow_opt_matches": "captain_hook.conditions",
    "workflow_opt_values": "captain_hook.conditions",
    "workflow_script_source": "captain_hook.conditions",
    "HookContext": "captain_hook.context",
    "AfterEdit": "captain_hook.contexts",
    "BeforeEdit": "captain_hook.contexts",
    "Excerpts": "captain_hook.contexts",
    "Introduced": "captain_hook.contexts",
    "PromptContext": "captain_hook.contexts",
    "WorkflowScriptSource": "captain_hook.contexts",
    "apply_contexts": "captain_hook.contexts",
    "excerpt_around": "captain_hook.contexts",
    "DurableSlot": "captain_hook.durable",
    "DurableState": "captain_hook.durable",
    "DurableStore": "captain_hook.durable",
    "BackgroundTask": "captain_hook.events",
    "BaseHookEvent": "captain_hook.events",
    "NotificationEvent": "captain_hook.events",
    "PermissionRequestEvent": "captain_hook.events",
    "PostToolUseEvent": "captain_hook.events",
    "PostToolUseFailureEvent": "captain_hook.events",
    "PreCompactEvent": "captain_hook.events",
    "PreToolUseEvent": "captain_hook.events",
    "SessionCron": "captain_hook.events",
    "SessionEndEvent": "captain_hook.events",
    "SessionStartEvent": "captain_hook.events",
    "StopEvent": "captain_hook.events",
    "SubagentStartEvent": "captain_hook.events",
    "SubagentStopEvent": "captain_hook.events",
    "ToolHookEvent": "captain_hook.events",
    "ToolRewriteEvent": "captain_hook.events",
    "UserPromptSubmitEvent": "captain_hook.events",
    "Deque": "captain_hook.fields",
    "File": "captain_hook.file",
    "categorize_files": "captain_hook.file",
    "file": "captain_hook.file",
    "block_command": "captain_hook.primitives.commands",
    "rewrite_command": "captain_hook.primitives.commands",
    "warn_command": "captain_hook.primitives.commands",
    "diff_lint": "captain_hook.primitives.lint",
    "lint": "captain_hook.primitives.lint",
    "GateVerdict": "captain_hook.primitives.llm",
    "NudgeVerdict": "captain_hook.primitives.llm",
    "PromptCheckVerdict": "captain_hook.primitives.llm",
    "llm_evaluate": "captain_hook.primitives.llm",
    "llm_gate": "captain_hook.primitives.llm",
    "llm_nudge": "captain_hook.primitives.llm",
    "prompt_check": "captain_hook.primitives.llm",
    "gate": "captain_hook.primitives.nudge",
    "nudge": "captain_hook.primitives.nudge",
    "SafetyVerdict": "captain_hook.primitives.permissions",
    "approve": "captain_hook.primitives.permissions",
    "deny": "captain_hook.primitives.permissions",
    "llm_approve": "captain_hook.primitives.permissions",
    "rewrite_code": "captain_hook.primitives.rewrite",
    "set_tool_input": "captain_hook.primitives.rewrite",
    "Artifact": "captain_hook.primitives.workflow",
    "Step": "captain_hook.primitives.workflow",
    "Workflow": "captain_hook.primitives.workflow",
    "text_matches": "captain_hook.primitives.workflow",
    "workflow": "captain_hook.primitives.workflow",
    "Prompt": "captain_hook.prompt",
    "SessionSlot": "captain_hook.session",
    "SessionStore": "captain_hook.session",
    "session_state": "captain_hook.session",
    "HooksSettings": "captain_hook.settings",
    "build_settings": "captain_hook.settings",
    "Clause": "captain_hook.signals.nlp",
    "NlpSignal": "captain_hook.signals.nlp",
    "Phrase": "captain_hook.signals.nlp",
    "has_nominal_subject": "captain_hook.signals.nlp",
    "is_past_predicate": "captain_hook.signals.nlp",
    "HookState": "captain_hook.state",
    "PrimitiveState": "captain_hook.state",
    "WorkflowState": "captain_hook.state",
    "workflow_state": "captain_hook.state",
    "style": "captain_hook.style",
    "Task": "captain_hook.tasks",
    "Tasks": "captain_hook.tasks",
    "Allow": "captain_hook.testing.types",
    "Ask": "captain_hook.testing.types",
    "Block": "captain_hook.testing.types",
    "FileFixture": "captain_hook.testing.types",
    "InlineTests": "captain_hook.testing.types",
    "Input": "captain_hook.testing.types",
    "Rewrite": "captain_hook.testing.types",
    "TranscriptFixture": "captain_hook.testing.types",
    "Warn": "captain_hook.testing.types",
    "Action": "captain_hook.types",
    "Agent": "captain_hook.types",
    "And": "captain_hook.types",
    "Content": "captain_hook.types",
    "CustomCommandLineCondition": "captain_hook.types",
    "CustomCondition": "captain_hook.types",
    "CustomInputTypeCondition": "captain_hook.types",
    "Event": "captain_hook.types",
    "FilePath": "captain_hook.types",
    "FromSubagent": "captain_hook.types",
    "HookResponse": "captain_hook.types",
    "HookResult": "captain_hook.types",
    "InPlanMode": "captain_hook.types",
    "Not": "captain_hook.types",
    "Or": "captain_hook.types",
    "Pattern": "captain_hook.types",
    "RanCommand": "captain_hook.types",
    "ReadFile": "captain_hook.types",
    "Runs": "captain_hook.types",
    "Signal": "captain_hook.types",
    "Signals": "captain_hook.types",
    "SkipPermissions": "captain_hook.types",
    "SourceEdits": "captain_hook.types",
    "TCondition": "captain_hook.types",
    "TestFile": "captain_hook.types",
    "Tool": "captain_hook.types",
    "ToolInput": "captain_hook.types",
    "TouchedFile": "captain_hook.types",
    "UsedSkill": "captain_hook.types",
    "Waiting": "captain_hook.types",
    "WorkflowScript": "captain_hook.types",
    "util": "captain_hook.util",
    "read_json": "captain_hook.util.fs",
    "resolve_binary": "captain_hook.util.fs",
    "Command": "cc_transcript.command",
    "CommandLine": "cc_transcript.command",
    "Redirect": "cc_transcript.command",
    "BashCall": "cc_transcript.tools",
    "EditCall": "cc_transcript.tools",
    "ExitPlanModeCall": "cc_transcript.tools",
    "GlobCall": "cc_transcript.tools",
    "GrepCall": "cc_transcript.tools",
    "MultiEditCall": "cc_transcript.tools",
    "NotebookEditCall": "cc_transcript.tools",
    "OtherCall": "cc_transcript.tools",
    "ReadCall": "cc_transcript.tools",
    "SkillCall": "cc_transcript.tools",
    "TaskCall": "cc_transcript.tools",
    "TaskCreateCall": "cc_transcript.tools",
    "TaskUpdateCall": "cc_transcript.tools",
    "ToolCall": "cc_transcript.tools",
    "ToolCallBase": "cc_transcript.tools",
    "WriteCall": "cc_transcript.tools",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> object:
    if (target := _EXPORTS.get(name)) is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(target)
    # Submodule entries (file/style/util) map a name to its own module path; every
    # other entry names an attribute defined in `target`.
    value = module if target == f"{__name__}.{name}" else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
