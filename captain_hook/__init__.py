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

from captain_hook import file, style, utils
from captain_hook.app import hook, on, register
from captain_hook.command import Command, CommandLine, Redirect
from captain_hook.context import HookContext
from captain_hook.events import (
    BaseHookEvent,
    NotificationEvent,
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreCompactEvent,
    PreToolUseEvent,
    SessionEndEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    ToolHookEvent,
    UserPromptSubmitEvent,
)
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
from captain_hook.signals.nlp import Clause, NlpSignal, Phrase
from captain_hook.state import HookState, PrimitiveState, WorkflowState, workflow_state
from captain_hook.tasks import Task, Tasks
from captain_hook.testing import Allow, Block, FileFixture, InlineTests, Input, Rewrite, TranscriptFixture, Warn
from captain_hook.types import (
    Action,
    Agent,
    Content,
    CustomCommandLineCondition,
    CustomCondition,
    CustomInputTypeCondition,
    Event,
    FilePath,
    HookResponse,
    HookResult,
    InPlanMode,
    Pattern,
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
from captain_hook.utils import read_json, resolve_binary
