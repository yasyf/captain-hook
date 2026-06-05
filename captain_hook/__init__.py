from __future__ import annotations

from captain_hook.app import hook, on, register
from captain_hook.cli import generate_settings, generate_settings_json
from captain_hook.command import Command, CommandLine, Redirect
from captain_hook.context import HookContext
from captain_hook.events import (
    BaseHookEvent,
    NotificationEvent,
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreCompactEvent,
    PreToolUseEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    ToolHookEvent,
    UserPromptSubmitEvent,
)
from captain_hook.file import File, PathMatcher, categorize_files
from captain_hook.primitives import (
    GateVerdict,
    NudgeVerdict,
    PromptCheckVerdict,
    audit,
    block_command,
    diff_lint,
    gate,
    lint,
    llm_gate,
    llm_nudge,
    nudge,
    prompt_check,
    session_id_for,
    styleguide,
    warn_command,
)
from captain_hook.primitives.llm import llm_evaluate
from captain_hook.prompt import Prompt, PromptMessage
from captain_hook.session import SessionSlot, SessionStore, session_state
from captain_hook.settings import AutoConf, HooksSettings, build_settings
from captain_hook.signals import cite_message, extract_signal_context, resolve_signals, score_signals, transcript_texts
from captain_hook.signals.nlp import Clause, NlpSignal, Phrase
from captain_hook.state import EchoTracker, HookState, PrimitiveState, workflow_state
from captain_hook.styleguide import StyleDiffRule, StyleRule, Violation
from captain_hook.tasks import Task, Tasks
from captain_hook.testing import Allow, Block, InlineTests, Input, TranscriptFixture, Warn
from captain_hook.tools import EditOp, TaskOp, WriteOp
from captain_hook.transcript import (
    ToolUse,
    ToolUseQuery,
    ToolUseSequence,
    Transcript,
    TranscriptMessage,
    TranscriptSlice,
    Turn,
)
from captain_hook.transcript.inputs import (
    AgentInput,
    BashInput,
    EditInput,
    FileInputBase,
    GenericInput,
    GlobInput,
    GrepInput,
    InputBase,
    ReadInput,
    SkillInput,
    TaskCreateInput,
    TaskUpdateInput,
    WriteInput,
    parse_tool_input,
)
from captain_hook.transcript.models import (
    ContentBlock,
    TextBlock,
    ToolResult,
    ToolUseBlock,
    parse_content_block,
)
from captain_hook.types import (
    Action,
    Agent,
    Content,
    CustomCondition,
    Event,
    FilePath,
    HookResult,
    HookSpec,
    InPlanMode,
    RanCommand,
    ReadFile,
    Signal,
    Signals,
    SourceEdits,
    TCondition,
    TestFile,
    Tool,
    TouchedFile,
    UsedSkill,
    Waiting,
)
from captain_hook.utils import read_json
from captain_hook.workflow import Artifact, Step, Workflow, text_matches
from captain_hook.workflow import workflow as workflow

__all__ = [
    # registration
    "hook",
    "on",
    "register",
    # events
    "Action",
    "Agent",
    "BaseHookEvent",
    "Content",
    "CustomCondition",
    "Event",
    "FilePath",
    "HookResult",
    "HookSpec",
    "InPlanMode",
    "NotificationEvent",
    "PostToolUseEvent",
    "PostToolUseFailureEvent",
    "PreCompactEvent",
    "PreToolUseEvent",
    "RanCommand",
    "ReadFile",
    "Signal",
    "Signals",
    "StopEvent",
    "SubagentStartEvent",
    "SubagentStopEvent",
    "TCondition",
    "TestFile",
    "Tool",
    "ToolHookEvent",
    "TouchedFile",
    "UsedSkill",
    "UserPromptSubmitEvent",
    "Waiting",
    # context
    "HookContext",
    "SessionSlot",
    "SessionStore",
    "session_state",
    "workflow_state",
    "EchoTracker",
    "HookState",
    "PrimitiveState",
    "SourceEdits",
    # primitives
    "audit",
    "block_command",
    "diff_lint",
    "gate",
    "GateVerdict",
    "lint",
    "llm_evaluate",
    "llm_gate",
    "llm_nudge",
    "nudge",
    "NudgeVerdict",
    "prompt_check",
    "PromptCheckVerdict",
    "session_id_for",
    "StyleDiffRule",
    "StyleRule",
    "styleguide",
    "Violation",
    "warn_command",
    # signals
    "cite_message",
    "extract_signal_context",
    "resolve_signals",
    "score_signals",
    "transcript_texts",
    "Clause",
    "NlpSignal",
    "Phrase",
    # commands / files
    "Command",
    "CommandLine",
    "File",
    "PathMatcher",
    "categorize_files",
    "Redirect",
    # prompts
    "Prompt",
    "PromptMessage",
    # settings / CLI
    "AutoConf",
    "HooksSettings",
    "build_settings",
    "generate_settings",
    "generate_settings_json",
    "read_json",
    # tools
    "EditOp",
    "TaskOp",
    "WriteOp",
    # tasks (native task store)
    "Task",
    "Tasks",
    # transcript
    "Transcript",
    "TranscriptMessage",
    "TranscriptSlice",
    "ToolUse",
    "ToolUseQuery",
    "ToolUseSequence",
    "Turn",
    "AgentInput",
    "BashInput",
    "ContentBlock",
    "EditInput",
    "FileInputBase",
    "GenericInput",
    "GlobInput",
    "GrepInput",
    "InputBase",
    "ReadInput",
    "SkillInput",
    "TaskCreateInput",
    "TaskUpdateInput",
    "TextBlock",
    "ToolResult",
    "ToolUseBlock",
    "WriteInput",
    "parse_content_block",
    "parse_tool_input",
    # workflow
    "Artifact",
    "Step",
    "Workflow",
    "text_matches",
    "workflow",
    # testing
    "Allow",
    "Block",
    "InlineTests",
    "Input",
    "TranscriptFixture",
    "Warn",
]
