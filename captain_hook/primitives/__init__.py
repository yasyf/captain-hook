from __future__ import annotations

from captain_hook.primitives.commands import (
    block_command,
    rewrite_command,
    rewrite_command_occurrences,
    warn_command,
)
from captain_hook.primitives.lint import diff_lint, lint
from captain_hook.primitives.llm import (
    GateVerdict,
    NudgeVerdict,
    PromptCheckVerdict,
    llm_evaluate,
    llm_gate,
    llm_nudge,
    prompt_check,
)
from captain_hook.primitives.nudge import gate, nudge
from captain_hook.primitives.permissions import SafetyVerdict, approve, deny, llm_approve
from captain_hook.primitives.rewrite import rewrite_code, set_tool_input
