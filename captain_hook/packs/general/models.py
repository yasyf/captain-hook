from __future__ import annotations

from dataclasses import dataclass

from captain_hook import (
    Agent,
    Allow,
    BaseHookEvent,
    Block,
    Event,
    Input,
    Rewrite,
    TaskCall,
    Tool,
    ToolInput,
    Warn,
    WorkflowScript,
    hook,
    llm_nudge,
    nudge,
    set_tool_input,
)


@dataclass(frozen=True, slots=True)
class DelegatedSpawn:
    """Gating context: the pending Agent/Task call's model pin, agent type, and prompt."""

    tag: str = "delegated_spawn"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if (call := evt.as_input(TaskCall)) is None or not call.prompt:
            return None
        model = call.model or "(none — inherits the session model, fable)"
        return f"model: {model}\nagent_type: {call.agent_type or '(default)'}\nprompt:\n{call.prompt}"


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

llm_nudge(
    """Decide whether this delegated subagent should run on opus-4.8 instead of fable-5.

<delegated_spawn> holds the pending Agent/Task call: its model pin (or that it inherits
the session model, fable), agent type, and prompt.

The Models rubric: implementation delegates to opus-4.8 at xhigh — opus is ~2x cheaper
than fable and nearly as capable. Fable's lanes are orchestration, review, hard
planning/design/diagnosis, all prose/writing, and implementation that is very sensitive
or error-prone (auth, migrations, concurrency, data loss, crypto, subtle algorithms).

Set fire=true only when the prompt is clearly routine implementation — building, fixing,
wiring, or refactoring code — with no fable-lane signal. A prompt that reviews, plans,
designs, diagnoses a hard bug, writes prose, or touches a sensitive surface stays on
fable: fire=false. When uncertain, fire=false — the agent may have chosen fable
deliberately, and a false alarm teaches it to ignore this nudge. Keep reasoning under
40 words.

<examples>
<example fire="true">
Implement the pagination endpoint in api/users.py per the spec in the plan.
Routine implementation with no sensitivity signal — the opus xhigh lane.
</example>
<example fire="true">
Add a --json flag to the export command and thread it through the formatter.
Well-scoped feature wiring; the default implementation lane.
</example>
<example fire="false">
Review the diff for correctness and concurrency issues.
Review is fable's lane.
</example>
<example fire="false">
Design the migration strategy for the sharded session store.
Hard planning/design stays on fable.
</example>
<example fire="false">
Implement the token-refresh race fix in the auth middleware.
Auth plus concurrency: sensitive, error-prone implementation stays on fable.
</example>
</examples>""",
    message=lambda r: (
        f"This delegation would run on fable, but it reads as routine implementation. {r.reasoning} "
        "Implementation defaults to model='opus' + effort='xhigh' (~2x cheaper, nearly as capable); "
        "a well-scoped edit to existing code can also go to gpt-5.5 via the codex skill behind a "
        "model='sonnet' low-effort wrapper. Keep fable if this genuinely is sensitive or error-prone. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[DelegatedSpawn()],
    events=Event.PreToolUse,
    only_if=[Tool("Agent|Task")],
    skip_if=[
        ToolInput("model", r"(?i)\b(opus|sonnet|haiku)\b"),
        Agent("Explore|claude-code-guide"),
    ],
    agent=False,
    transcript=False,
    tests={
        Input(prompt="implement the pagination endpoint in api/users.py"): Warn(pattern="opus"),
        Input(model="fable", prompt="add a --json flag to the export command"): Warn(pattern="opus"),
        Input(model="opus", prompt="implement the pagination endpoint in api/users.py"): Allow(),
        Input(model="sonnet", prompt="scan the repo for TODO markers"): Allow(),
        Input(agent_type="Explore", prompt="find where the config loader lives"): Allow(),
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
