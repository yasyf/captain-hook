from __future__ import annotations

from captain_hook.app import hook, on, register
from captain_hook.cli import generate_settings, generate_settings_json
from captain_hook.utils import read_json
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
from captain_hook.file import File, PathMatcher
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
    warn_command,
)
from captain_hook.primitives.llm import llm_evaluate
from captain_hook.prompt import Prompt, PromptMessage
from captain_hook.session import SessionSlot, SessionStore, session_state
from captain_hook.settings import AutoConf, HooksSettings, build_settings
from captain_hook.signals import cite_message, extract_signal_context, resolve_signals, score_signals, transcript_texts
from captain_hook.signals.nlp import Clause, NlpSignal, Phrase
from captain_hook.state import HookState, PrimitiveState
from captain_hook.testing import Allow, Block, Input, TranscriptFixture, Warn
from captain_hook.testing import TTest as TTest
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
)
from captain_hook.types import RegisteredHook as RegisteredHook
from captain_hook.types import Signal as Signal
from captain_hook.types import Signals as Signals
from captain_hook.types import TCondition as TCondition
from captain_hook.types import TestFile as TestFile
from captain_hook.types import Tool as Tool
from captain_hook.types import TouchedFile as TouchedFile
from captain_hook.types import UsedSkill as UsedSkill
from captain_hook.types import tokens_to_regex as tokens_to_regex
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
    "RegisteredHook",
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
    # context
    "HookContext",
    "SessionSlot",
    "SessionStore",
    "session_state",
    "HookState",
    "PrimitiveState",
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
    "warn_command",
    # signals
    "cite_message",
    "extract_signal_context",
    "resolve_signals",
    "score_signals",
    "tokens_to_regex",
    "transcript_texts",
    "Clause",
    "NlpSignal",
    "Phrase",
    # commands / files
    "Command",
    "CommandLine",
    "File",
    "PathMatcher",
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
    "Input",
    "TranscriptFixture",
    "TTest",
    "Warn",
]
