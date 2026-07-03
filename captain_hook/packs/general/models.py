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
    skip_if=[
        ToolInput(
            "prompt",
            r"(?i)\b(classif|label|tag|categoriz|per.file|one (fact|thing)|mechanical|probe|ping"
            r"|echo|smoke|count|capacit|extract)",
        )
    ],
    message=(
        "This subagent is pinned to haiku. Route a subagent to haiku only for a single-fact "
        "mechanical step — classifying, labeling, tagging, counting, or probing one thing per "
        "item. Anything that carries judgment should run on sonnet; use model='sonnet', or drop "
        "model entirely to inherit the session model. If this genuinely is a mechanical "
        "single-fact step, say 'mechanical' in the prompt and retry — the haiku pin will be "
        "allowed. See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    block=True,
    tests={
        Input(model="haiku", prompt="implement the retry backoff in the client"): Block(),
        Input(model="haiku", prompt="classify each file's language"): Allow(),
        Input(model="haiku", prompt="Probe subagent capacity: spawn and return the word ok"): Allow(),
        Input(model="haiku", prompt="mechanical step: return the repo's default branch name"): Allow(),
        Input(model="haiku", prompt="count the TODO markers in src/"): Allow(),
        Input(prompt="implement the retry backoff in the client"): Allow(),
        Input(model="sonnet", prompt="implement the retry backoff in the client"): Allow(),
    },
)

hook(
    Event.PreToolUse,
    only_if=[
        Tool("Agent|Task"),
        ToolInput("model", r"(?i)\b(haiku|sonnet|opus)\b"),
        ToolInput(
            "prompt",
            r"(?i)\b(writ(e|es|ing|ten)|draft|redraft|rewrit|revis|polish|copyedit|compose|author|update|edit)",
        ),
        ToolInput(
            "prompt",
            r"(?i)\b(readme|docs?|documentation|blog|changelog|release notes|announcement"
            r"|tutorial|guide|prose|copywrit|marketing copy|article|newsletter|email"
            r"|pr description|commit message)\b",
        ),
    ],
    skip_if=[ToolInput("prompt", r"(?i)\b(classif|label|tag|categoriz|count|extract|mechanical)")],
    message=(
        "This subagent is pinned to a non-fable model but its prompt is prose/writing work. "
        "All writing — docs, READMEs, release notes, any user-facing text — routes to fable: "
        "drop model to inherit the session model, or pass model='fable'. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    block=True,
    tests={
        Input(model="sonnet", prompt="Write the README quickstart for this repo"): Block(),
        Input(model="opus", prompt="draft the release notes for v2"): Block(),
        Input(model="haiku", prompt="update the CHANGELOG entry for the fix"): Block(),
        Input(model="fable", prompt="write the README quickstart"): Allow(),
        Input(prompt="write the README quickstart"): Allow(),
        Input(model="sonnet", prompt="review the README for factual errors"): Allow(),
        Input(model="sonnet", prompt="update the retry backoff config"): Allow(),
        Input(model="haiku", prompt="label each README section with its Diataxis mode"): Allow(),
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

nudge(
    """
    This workflow script pins a non-fable model on what looks like prose/writing stages. All
    writing — docs, READMEs, release notes, any user-facing text — routes to fable: inherit the
    session model or pin model: 'fable'. See CLAUDE.md § Plan Execution & Orchestration (Models).
    """,
    only_if=[
        Tool("Workflow"),
        WorkflowScript(
            pattern=r"(?i)\b(readme|docs?|documentation|blog|changelog|release notes|prose)\b",
            model="haiku|sonnet|opus",
        ),
    ],
    events=Event.PreToolUse,
    max_fires=2,
    tests={
        Input(script="steps:\n  - agent: write the README intro\n    model: 'sonnet'\n"): Warn(),
        Input(script="steps:\n  - agent: write the README intro\n    model: 'fable'\n"): Allow(),
        Input(script="steps:\n  - agent: fix the retry backoff\n    model: 'sonnet'\n"): Allow(),
    },
)
