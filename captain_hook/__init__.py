from __future__ import annotations

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
from captain_hook.command import CommandLine, ParsedCommand, Redirect
from captain_hook.context import HookContext
from captain_hook.contexts import AfterEdit, BeforeEdit, Introduced, PromptContext, apply_contexts
from captain_hook.durable import DurableSlot, DurableState, DurableStore
from captain_hook.events import (
    BaseHookEvent,
    NotificationEvent,
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreCompactEvent,
    PreToolUseEvent,
    SessionEndEvent,
    SessionStartEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    ToolHookEvent,
    UserPromptSubmitEvent,
)
from captain_hook.fields import Deque
from captain_hook.file import File, categorize_files
from captain_hook.primitives import (
    GateVerdict,
    NudgeVerdict,
    PromptCheckVerdict,
    block_command,
    gate,
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
from captain_hook.testing import Allow, Block, FileFixture, InlineTests, Input, Rewrite, TranscriptFixture, Warn
from captain_hook.types import (
    Action,
    Agent,
    And,
    Command,
    Content,
    CustomCommandLineCondition,
    CustomCondition,
    CustomInputTypeCondition,
    Event,
    FilePath,
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
