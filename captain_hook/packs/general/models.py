from __future__ import annotations

from captain_hook import (
    Agent,
    Allow,
    Block,
    Event,
    Input,
    Rewrite,
    Tool,
    ToolInput,
    Warn,
    WorkflowScript,
    hook,
    nudge,
    set_tool_input,
)

hook(
    Event.PreToolUse,
    only_if=[Tool("Agent|Task"), ToolInput("model", r"(?i)\bhaiku\b")],
    skip_if=[ToolInput("prompt", r"(?i)\b(classif|label|tag|categoriz|per.file|one (fact|thing))")],
    message=(
        "This subagent is pinned to haiku. Route a subagent to haiku only for a single-fact "
        "mechanical step — classifying, labeling, or tagging one thing per item. Anything that "
        "carries judgment should run on sonnet; use model='sonnet', or drop model entirely to "
        "inherit the session model. See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    block=True,
    tests={
        Input(model="haiku", prompt="implement the retry backoff in the client"): Block(),
        Input(model="haiku", prompt="classify each file's language"): Allow(),
        Input(prompt="implement the retry backoff in the client"): Allow(),
        Input(model="sonnet", prompt="implement the retry backoff in the client"): Allow(),
    },
)

set_tool_input(
    "model",
    "sonnet",
    tool="Agent|Task",
    only_if=[Agent("Explore|claude-code-guide")],
    note=(
        "Upgraded this recon subagent from the silent haiku default to sonnet, per the Models "
        "table in CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    tests={
        Input(agent_type="Explore"): Rewrite(model="sonnet"),
        Input(agent_type="Explore", model="haiku"): Allow(),
        Input(agent_type="general-purpose"): Allow(),
    },
)

nudge(
    """
    This workflow script pins agent() steps to haiku. Reserve haiku for mechanical single-fact
    map steps; a judgment-bearing stage should inherit the session model or route up. See
    CLAUDE.md § Plan Execution & Orchestration (Models).
    """,
    only_if=[Tool("Workflow"), WorkflowScript(model="haiku")],
    events=Event.PreToolUse,
    max_fires=2,
    tests={
        Input(script="steps:\n  - agent: reviewer\n    model: 'haiku'\n"): Warn(),
        Input(script="steps:\n  - agent: reviewer\n    model: 'sonnet'\n"): Allow(),
    },
)
