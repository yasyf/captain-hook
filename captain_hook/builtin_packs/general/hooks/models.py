from __future__ import annotations

import re
from dataclasses import dataclass

from captain_hook import (
    Agent,
    Allow,
    And,
    BaseHookEvent,
    Block,
    Clause,
    Confirm,
    Event,
    FilePath,
    FromSubagent,
    Input,
    Not,
    Or,
    Phrase,
    Prompt,
    Rewrite,
    T,
    TaskCall,
    TestFile,
    Tool,
    ToolInput,
    Warn,
    WorkflowScript,
    WorkflowScriptSource,
    hook,
    llm_gate,
    llm_nudge,
    nudge,
    set_tool_input,
)
from captain_hook.contexts import WORKFLOW_SCRIPT_CAP
from captain_hook.langs import LANG_GLOBS
from captain_hook.signals.nlp import nlp_scan

DELIVERABLE_NUDGE_RUBRIC = str(Prompt.load("fragments/deliverable_rubric", verdict_attr="fire"))
WORKFLOW_HEADER = str(Prompt.load("fragments/workflow_script_header"))
REVIEW_ROUTING_PATTERN = (
    r"(?i)(\b(review|refut|adversari|audit|correctness|diagnos|root.?caus|secur|vuln|pentest)"
    r"|\bverif\w*[\s\S]{0,160}?\b(auth|crypt|secret|sanitiz|inject|input.?valid|token|session))"
)
WRITING_VERBS = (
    "write",
    "draft",
    "redraft",
    "rewrite",
    "revise",
    "reword",
    "polish",
    "copyedit",
    "compose",
    "author",
    "update",
    "edit",
)
INLINE_EDIT_MIN_CHARS = 400
SUSTAINED_BROWSER_CALLS = 5
BROWSER_DRIVER = re.compile(r"(?i)\b(agent-browser|playwright)\b")
CODEX_AGENTS = ("codex-wrapper", "codex:codex-wrapper", "codex-wrapper-async", "codex:codex-wrapper-async")
SOURCE_FILE_GLOBS = tuple(
    pattern for language, patterns in LANG_GLOBS.items() if language != "md" for pattern in patterns
)


def prose_deliverable_sentences(text: str) -> list[str]:
    """Sentences where a writing verb governs a prose artifact, minus negated asks ("do NOT edit CHANGELOG.md").

    The text is de-noised for the tagger first: path/URL tokens dropped, brackets and
    word-edge quotes blanked (so `agent('write …')` doesn't glue into one token; intra-word
    apostrophes survive for "n't" negations), readme/changelog extensions stripped, intra-word
    hyphens split, writing verbs lowercased, and imperative writing verbs given a determiner —
    "Update CHANGELOG.md" otherwise parses as a noun compound.
    """
    verbs = "|".join(WRITING_VERBS)
    text = re.sub(r"\S+/\S+", " ", text)
    text = re.sub(r"[(){}\[\]<>]|(?<![A-Za-z])['\"`]|['\"`](?![A-Za-z])", " ", text)
    text = re.sub(r"(?i)\b(readme|changelog)\.(?:md|rst|txt)\b", r"\1", text)
    text = re.sub(r"(?<=\w)-(?=\w)", " ", text)
    text = re.sub(rf"(?i)\b({verbs})\b", lambda m: m.group(1).lower(), text)
    text = re.sub(rf"\b({verbs})[ \t]+((?i:readme|changelog|docs))\b", r"\1 the \2", text)
    artifact = Phrase(
        "readme",
        "readme.md",
        "doc",
        "docs",
        "documentation",
        "changelog",
        "changelog.md",
        "blog",
        "blog post",
        "release note",
        "announcement",
        "tutorial",
        "guide",
        "prose",
        "marketing copy",
        "article",
        "newsletter",
        "email",
        "pr description",
        "commit message",
    )
    writing = Phrase(*WRITING_VERBS)
    if not (matched := nlp_scan([Clause(noun=artifact, verb=writing)], text)):
        return []
    negated = set(nlp_scan([Clause(noun=artifact, verb=writing, negated=True)], text))
    return [s for s in matched if s not in negated]


def browser_calls(
    n: int, *, tool: str = "Bash", field: str = "command", value: str = "agent-browser click '#next'"
) -> list[dict[str, object]]:
    """A same-turn run of ``n`` browser tool_use lines (Bash by default) for the delegation-nudge inline tests."""
    return [T.assistant(T.tool(tool, **{field: value})) for _ in range(n)]


def turn_browser_call_count(evt: BaseHookEvent) -> int:
    calls = evt.ctx.t.current_turn.tool_calls
    return (
        calls.named("Bash").where_input(command=BROWSER_DRIVER).count()
        + calls.named("Skill").where_input(skill=BROWSER_DRIVER).count()
    )


@dataclass(frozen=True, slots=True)
class DelegatedSpawn:
    """Gating context: the pending Agent/Task call's model pin, agent type, and prompt."""

    tag: str = "delegated_spawn"
    required: bool = True
    unpinned_note: str = "(none — inherits the session model, opus)"

    def content(self, evt: BaseHookEvent) -> str | None:
        if (call := evt.as_input(TaskCall)) is None or not call.prompt:
            return None
        model = call.model or self.unpinned_note
        return f"model: {model}\nagent_type: {call.agent_type or '(default)'}\nprompt:\n{call.prompt}"


@dataclass(frozen=True, slots=True)
class InlineEdit:
    """Gating context: the file the main agent is about to edit inline on the main loop."""

    tag: str = "edit_target"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if not evt.file or evt.content is None:
            return None
        return f"file: {evt.file.path}\nincoming text: {len(evt.content):,} chars"


@dataclass(frozen=True, slots=True)
class ProseSpawn(DelegatedSpawn):
    """Gating context: the pending spawn, present only when its prompt asks to produce a prose artifact."""

    unpinned_note: str = "(none — an unpinned subagent runs opus)"

    def content(self, evt: BaseHookEvent) -> str | None:
        # WORKAROUND: zero-arg super() breaks under @dataclass(slots=True), which rebuilds the class.
        if (base := DelegatedSpawn.content(self, evt)) is None or (call := evt.as_input(TaskCall)) is None:
            return None
        if not (sentences := prose_deliverable_sentences((call.prompt or "")[:WORKFLOW_SCRIPT_CAP])):
            return None
        matched = "\n".join(f"  {s[:300]}" for s in sentences)
        return f"{base}\n\nsentences the prose prefilter matched:\n{matched}"


@dataclass(frozen=True, slots=True)
class ProseWorkflowScript(WorkflowScriptSource):
    """Gating context: the script source, present only when its prose asks survive the prefilter."""

    def content(self, evt: BaseHookEvent) -> str | None:
        if (parts := self.pins_and_source(evt)) is None:
            return None
        header, source = parts
        if not (sentences := prose_deliverable_sentences(source)):
            return None
        matched = "\n".join(f"  {s[:300]}" for s in sentences)
        return f"{header}\n\nsentences the prose prefilter matched:\n{matched}\n\n{source}"


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
        "Haiku runs only single-fact mechanical steps such as classifying, labeling, or counting one thing per item. "
        "Drop the `model` pin so the spawn runs opus, or pin `model='sonnet'` when the lane calls for sonnet."
    ),
    block=True,
    confirm=Confirm(rule="A haiku-pinned spawn whose task needs judgment beyond one mechanical fact per item."),
    tests={
        Input(model="haiku", prompt="implement the retry backoff in the client"): Block(pattern="Drop the `model` pin"),
        Input(model="haiku", prompt="implement the retry backoff"): Block(pattern="single-fact mechanical"),
        Input(model="haiku", prompt="classify each file's language"): Allow(),
        Input(model="haiku", prompt="Probe subagent capacity: spawn and return the word ok"): Allow(),
        Input(model="haiku", prompt="mechanical step: return the repo's default branch name"): Allow(),
        Input(model="haiku", prompt="count the TODO markers in src/"): Allow(),
        Input(prompt="implement the retry backoff in the client"): Allow(),
        Input(model="sonnet", prompt="implement the retry backoff in the client"): Allow(),
    },
)

llm_gate(
    Prompt.load(
        "models/prose_spawn_gate",
        deliverable_rubric=str(Prompt.load("fragments/deliverable_rubric", verdict_attr="block")),
    ),
    message=(
        "Prose deliverables are written by Claude Opus, never by codex, astra, sonnet, haiku, or fable. "
        "Re-spawn with `model='opus'` or no `model` pin, as a Claude agent that writes the prose itself."
    ),
    contexts=[ProseSpawn()],
    events=Event.PreToolUse,
    only_if=[Tool("Agent|Task")],
    skip_if=[
        And(
            Not(ToolInput("model", r"(?i)\b(sonnet|haiku|fable)\b")),
            Not(ToolInput("prompt", r"(?i)\b(codex|astra)\b")),
            Not(Agent(*CODEX_AGENTS)),
        ),
        ToolInput("prompt", r"(?i)\b(classif|label|tag|categoriz|count|extract|mechanical)"),
        Agent("Explore|claude-code-guide"),
    ],
    agent=False,
    transcript=False,
    max_context=16_000,
    tests={
        Input(model="sonnet", prompt="Write the README quickstart for this repo"): Block(),
        Input(model="haiku", prompt="update the CHANGELOG entry for the fix"): Block(),
        Input(model="fable", prompt="write the README quickstart"): Block(),
        Input(
            agent_type="codex:codex-wrapper",
            prompt="Rewrite the README quickstart in the technical-builder voice",
        ): Block(),
        Input(
            model="opus",
            prompt="Orchestrate the incident-retro revision: rewrite the release notes and redraft "
            "the guide. Every sentence is written by gpt-6-astra at xhigh via the codex skill and "
            "landed verbatim; you orchestrate and never write the prose yourself.",
        ): Block(),
        Input(model="opus", prompt="draft the release notes for v2"): Allow(),
        Input(model="claude-opus-5-5", prompt="Write the README quickstart for this repo"): Allow(),
        Input(prompt="write the README quickstart"): Allow(),
        Input(prompt="draft the release notes for v2"): Allow(),
        Input(
            agent_type="ship-pr",
            prompt="Write the PR description below to a body file and open the PR.\ntitle: fix retry\nbody: ...",
        ): Allow(),
        Input(
            agent_type="ship-pr",
            model="opus",
            prompt="Write the PR description below to a body file and open the PR.\ntitle: fix retry\nbody: ...",
        ): Allow(),
        Input(
            model="opus",
            prompt="Fix the import in cli.py, have the codex skill review the diff, then draft the "
            "CHANGELOG entry yourself.",
            llm={"block": False},
        ): Allow(),
        Input(model="sonnet", prompt="review the README for factual errors"): Allow(),
        Input(model="sonnet", prompt="update the retry backoff config"): Allow(),
        Input(model="haiku", prompt="label each README section with its Diataxis mode"): Allow(),
        Input(agent_type="Explore", model="sonnet", prompt="find where the README quickstart is written"): Allow(),
        Input(agent_type="claude-code-guide", model="sonnet", prompt="explain how the docs get updated"): Allow(),
        Input(
            model="sonnet",
            prompt="Fix the failing test in cli.py. Do NOT edit the CHANGELOG — a sibling owns updating it",
        ): Allow(),
        Input(
            agent_type="codex:codex-wrapper",
            prompt="Review the diff for correctness; return findings as file:line JSON",
        ): Allow(),
    },
)

set_tool_input(
    "model",
    "sonnet",
    tool="Agent|Task",
    only_if=[Agent("Explore|claude-code-guide")],
    note="Recon subagents run on sonnet, not the haiku default. Pinned `model='sonnet'` on this spawn.",
    tests={
        Input(agent_type="Explore"): Rewrite(model="sonnet"),
        Input(agent_type="Explore", model="haiku"): Allow(),
        Input(agent_type="general-purpose"): Allow(),
    },
)

llm_nudge(
    Prompt.load("models/implementation_spawn_nudge"),
    message=(
        "Implementation subagents run on `model='opus'` and repetitive N-unit sweeps on gpt-6.1-sol via "
        "`codex:codex-wrapper`; fable is only for the most sensitive code. "
        "Re-spawn with that route, at `effort='high'` for a bounded change and `effort='xhigh'` otherwise."
    ),
    contexts=[DelegatedSpawn()],
    events=Event.PreToolUse,
    only_if=[Tool("Agent|Task")],
    skip_if=[
        ToolInput("model", r"(?i)\b(opus|sonnet|haiku)\b"),
        Agent("Explore|claude-code-guide|codex-wrapper|codex:codex-wrapper"),
    ],
    agent=False,
    transcript=False,
    tests={
        Input(prompt="implement the pagination endpoint in api/users.py"): Warn(pattern="opus"),
        Input(model="fable", prompt="add a --json flag to the export command"): Warn(pattern="opus"),
        Input(prompt="add a retry wrapper around the upload call in api/files.py"): Warn(pattern="opus"),
        Input(prompt="Build out the new ingestion subsystem: parser, store, and CLI wiring, shape TBD"): Warn(
            pattern="opus"
        ),
        Input(
            prompt="Convert the eleven test modules under tests/legacy/ to pytest, one per lane, per the worked example"
        ): Warn(pattern="gpt-6.1-sol"),
        Input(model="opus", prompt="implement the pagination endpoint in api/users.py"): Allow(),
        Input(model="sonnet", prompt="scan the repo for TODO markers"): Allow(),
        Input(agent_type="Explore", prompt="find where the config loader lives"): Allow(),
        Input(agent_type="codex:codex-wrapper", prompt="Apply the edit described here to utils/backoff.py"): Allow(),
    },
)

llm_nudge(
    Prompt.load("models/inline_edit_nudge"),
    message=(
        "Sizable implementation is delegated, not edited inline on the main loop. "
        "Spawn an `Agent` with `model='opus'`, or a typed `model='fable'` subagent for the most sensitive code, "
        "and hand it this change."
    ),
    contexts=[InlineEdit()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Edit|Write|MultiEdit"),
        FilePath(*SOURCE_FILE_GLOBS),
    ],
    skip_if=[TestFile(), FromSubagent()],
    when=lambda evt: len(evt.content or "") >= INLINE_EDIT_MIN_CHARS,
    max_fires=1,
    agent=False,
    transcript=False,
    tests={
        Input(
            file="src/api/users.py",
            content="def list_users(page: int):\n    return paginate(page)\n" * 12,
        ): Warn(pattern="opus"),
        Input(
            file="src/core/cache.py",
            content="def get(key: str):\n    return store.lookup(key)\n" * 12,
        ): Warn(pattern="opus"),
        Input(
            file="README.md",
            content="Pagination lands in the users API.\n" * 20,
        ): Allow(),
        Input(file="src/api/users.py", old="page = 1", content="page = 2"): Allow(),
        Input(
            file="src/api/users.py",
            content="def list_users(page: int):\n    return paginate(page)\n" * 12,
            agent_id="tm1",
        ): Allow(),
        Input(
            file="tests/test_users.py",
            content="def test_list_users(page: int):\n    assert paginate(page)\n" * 12,
        ): Allow(),
        Input(
            file="src/auth/middleware.py",
            content="def refresh_token(lock: Lock):\n    with lock:\n        rotate()\n" * 12,
            llm={"fire": False},
        ): Allow(),
    },
)


llm_nudge(
    Prompt.load("models/browser_delegation_nudge"),
    message=(
        "Sustained browser automation runs in a subagent, not inline on the main loop. "
        "Spawn an `Agent` with `model='opus'` and `effort='xhigh'` to drive `agent-browser` and return findings."
    ),
    events=Event.PostToolUse,
    only_if=[
        Tool("Bash|Skill"),
        Or(ToolInput("command", BROWSER_DRIVER.pattern), ToolInput("skill", BROWSER_DRIVER.pattern)),
    ],
    skip_if=[FromSubagent()],
    when=lambda evt: turn_browser_call_count(evt) >= SUSTAINED_BROWSER_CALLS,
    max_fires=1,
    agent=False,
    transcript=True,
    tests={
        Input(command="agent-browser click '#submit'", transcript=browser_calls(5)): Warn(pattern="model='opus'"),
        Input(command="npx agent-browser click '#next'", transcript=browser_calls(5)): Warn(pattern="model='opus'"),
        Input(command="playwright-cli click e15", transcript=browser_calls(5)): Warn(pattern="model='opus'"),
        Input(
            tool="Skill",
            tool_input={"skill": "agent-browser-with-cookies"},
            transcript=browser_calls(5),
        ): Warn(pattern="model='opus'"),
        Input(
            command="agent-browser click '#submit'",
            transcript=browser_calls(3) + browser_calls(2, tool="Skill", field="skill", value="agent-browser"),
        ): Warn(pattern="model='opus'"),
        Input(command="agent-browser click '#submit'", agent_id="tm1", transcript=browser_calls(5)): Allow(),
        Input(command="agent-browser screenshot out.png"): Allow(),
        Input(command="agent-browser click '#submit'", transcript=browser_calls(5), llm={"fire": False}): Allow(),
        Input(command="ls -la", transcript=browser_calls(5)): Allow(),
    },
)

llm_nudge(
    Prompt.load("models/review_routing_spawn_nudge"),
    label="review_routing_spawn",
    message=(
        "Code review, security audit, and bug diagnosis route to gpt-6.1-sol through codex, not a Claude subagent. "
        "Spawn `subagent_type: 'codex:codex-wrapper'` with the self-contained question, or run `Skill(codex)` "
        "from the main conversation."
    ),
    contexts=[DelegatedSpawn()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Agent|Task"),
        ToolInput("prompt", REVIEW_ROUTING_PATTERN),
    ],
    skip_if=[
        And(
            ToolInput("model", r"(?i)\b(opus|sonnet|haiku)\b"),
            Not(ToolInput("prompt", r"(?i)\bcodex\b")),
        ),
        Agent("Explore|claude-code-guide|codex-wrapper|codex:codex-wrapper"),
    ],
    agent=False,
    transcript=False,
    tests={
        Input(prompt="Review the diff for correctness and concurrency issues"): Warn(pattern="gpt-6.1-sol"),
        Input(model="fable", prompt="Adversarially refute this finding: the retry loop is wrong"): Warn(
            pattern="codex"
        ),
        Input(model="sonnet", prompt="Review the diff for correctness via the codex skill"): Warn(
            pattern="codex-wrapper"
        ),
        Input(
            agent_type="codex:codex-wrapper", prompt="Review the diff for correctness; return findings as JSON"
        ): Allow(),
        Input(model="sonnet", prompt="Review the diff for correctness and concurrency"): Allow(),
        Input(prompt="fix the failing import in cli.py"): Allow(),
        Input(agent_type="Explore", prompt="find where the review pipeline lives"): Allow(),
        Input(
            prompt="Synthesize the confirmed review findings and decide which to fix",
            llm={"fire": False},
        ): Allow(),
        Input(
            agent_type="codex:codex-wrapper",
            prompt="Synthesize the confirmed review findings and decide which to fix",
        ): Allow(),
        Input(
            model="opus",
            prompt="Synthesize the confirmed review findings and decide which to fix",
        ): Allow(),
        Input(prompt="Audit auth/session.py for security vulnerabilities"): Warn(pattern="gpt-6.1-sol"),
        Input(prompt="Verify the input-validation change blocks path traversal"): Warn(pattern="codex"),
        Input(prompt="Verify the pagination change renders the last page correctly"): Allow(),
        Input(
            prompt="Implement mitigations for the security audit findings in auth.py",
            llm={"fire": False},
        ): Allow(),
        Input(
            prompt="Escalation: the codex:codex-wrapper review of this diff returned no findings "
            "despite the reproduced double-close — re-review src/pool.go and report findings as JSON",
            llm={"fire": False},
        ): Allow(),
        Input(
            prompt="Escalation: the codex-wrapper review returned nothing — run the codex skill "
            "to re-review the diff for correctness"
        ): Warn(pattern="codex-wrapper"),
    },
)

nudge(
    "Haiku runs only single-fact mechanical `agent()` steps. "
    "Drop the `model: 'haiku'` pin from judgment-bearing stages so they run opus.",
    only_if=[Tool("Workflow"), WorkflowScript(model="haiku")],
    events=Event.PreToolUse,
    max_fires=2,
    tests={
        Input(script="steps:\n  - agent: reviewer\n    model: 'haiku'\n"): Warn(),
        Input(script="steps:\n  - agent: reviewer\n    model: 'sonnet'\n"): Allow(),
    },
)

llm_nudge(
    Prompt.load(
        "models/prose_workflow_nudge",
        workflow_script_header=WORKFLOW_HEADER,
        deliverable_rubric=DELIVERABLE_NUDGE_RUBRIC,
    ),
    message=(
        "Workflow prose stages are written by Claude Opus, never by codex, astra, sonnet, haiku, or fable. "
        "Pin each prose stage `model: 'opus'` or leave it unpinned, with no `codex:codex-wrapper` agentType."
    ),
    contexts=[ProseWorkflowScript()],
    events=Event.PreToolUse,
    only_if=[Tool("Workflow")],
    skip_if=[
        And(
            Not(WorkflowScript(model=r"(?i)sonnet|haiku|fable")),
            Not(WorkflowScript(pattern=r"(?i)\b(codex|astra)\b")),
        )
    ],
    max_fires=2,
    max_context=16_000,
    agent=False,
    transcript=False,
    tests={
        Input(script="steps:\n  - agent: write the README intro\n    model: 'sonnet'\n"): Warn(pattern="opus"),
        Input(script="steps:\n  - agent: write the README intro\n    model: 'fable'\n"): Warn(pattern="opus"),
        Input(script="agent('Rewrite the README quickstart', {agentType: 'codex:codex-wrapper'})"): Warn(
            pattern="opus"
        ),
        Input(script="steps:\n  - agent: write the README intro\n"): Allow(),
        Input(script="agent('Rewrite the README quickstart', {model: 'opus'})"): Allow(),
        Input(script="steps:\n  - agent: fix the retry backoff\n    model: 'sonnet'\n"): Allow(),
        Input(script="agent('Audit docs/architecture.md for stale claims', {model: 'opus'})"): Allow(),
        Input(script="agent('recon the module map', {model: 'sonnet'})\n// every prose stage runs on opus\n"): Allow(),
        Input(
            script="agent('Fix the import in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'opus'})",
        ): Allow(),
        Input(
            script="agent('Review the diff for correctness', {agentType: 'codex:codex-wrapper'})\n"
            "agent('Write the CHANGELOG entry for the fix', {model: 'opus'})",
            llm={"fire": False},
        ): Allow(),
    },
)

llm_nudge(
    Prompt.load(
        "models/review_routing_workflow_nudge",
        workflow_script_header=WORKFLOW_HEADER,
        deliverable_rubric=DELIVERABLE_NUDGE_RUBRIC,
    ),
    label="review_routing_workflow",
    message=(
        "Workflow review, security-audit, and bug-diagnosis stages route to gpt-6.1-sol through codex. "
        "Give each such stage `agentType: 'codex:codex-wrapper'` with the self-contained question as its prompt."
    ),
    contexts=[WorkflowScriptSource()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Workflow"),
        WorkflowScript(pattern=REVIEW_ROUTING_PATTERN),
    ],
    max_fires=2,
    max_context=16_000,
    agent=False,
    transcript=False,
    tests={
        Input(script="const findings = await agent(`Sweep the diff for correctness issues; return JSON`)"): Warn(
            pattern="codex"
        ),
        Input(script="agent(`Adversarially refute: ${f.title}`, {model: 'fable', effort: 'max'})"): Warn(
            pattern="gpt-6.1-sol"
        ),
        Input(
            script="agent('Write a self-contained codex prompt reviewing this diff, "
            "then run the codex skill', {model: 'sonnet', effort: 'low'})",
        ): Warn(pattern="codex-wrapper"),
        Input(
            script="agent(`Review the diff hunks in src/ for correctness; return findings as JSON`, "
            "{agentType: 'codex:codex-wrapper'})",
            llm={"fire": False},
        ): Allow(),
        Input(script="agent('fix the failing import in cli.py')"): Allow(),
        Input(
            script="agent(`Synthesize the confirmed review findings and decide which to fix`)",
            llm={"fire": False},
        ): Allow(),
        Input(
            script="agent(`Synthesize the confirmed review findings and decide which to fix`, "
            "{agentType: 'codex:codex-wrapper', effort: 'xhigh'})",
            llm={"fire": False},
        ): Allow(),
        Input(
            script="agent(`Synthesize the confirmed review findings and decide which to fix`, "
            "{model: 'opus', effort: 'xhigh'})",
            llm={"fire": False},
        ): Allow(),
        Input(script="agent(`Audit the login flow for auth bypass and injection; return findings as JSON`)"): Warn(
            pattern="gpt-6.1-sol"
        ),
        Input(script="agent('Verify the CLI renders the last page correctly')"): Allow(),
        Input(
            script=(
                "const solOrOpus = async (prompt, key) => {\n"
                "  const r = await agent(prompt, { agentType: 'codex:codex-wrapper', "
                "label: `${key}:sol`, phase: 'Review', schema: REVIEW })\n"
                "  if (r) return { ...r, lane_model: 'sol' }\n"
                "  log(`${key}: sol empty — opus fallback`)\n"
                "  const f = await agent(prompt, { label: `${key}:opus`, phase: 'Review', schema: REVIEW })\n"
                "  return f ? { ...f, lane_model: 'opus' } : null\n"
                "}"
            ),
            llm={"fire": False},
        ): Allow(),
        Input(
            script="export const meta = { description: 'refuter pass; sol lane unavailable — "
            "opus escalation per models table' }\n"
            "const f = await agent(`Adversarially refute: ${finding.title}`)",
            llm={"fire": False},
        ): Allow(),
        Input(
            script="const findings = await agent(`Review the diff in src/ for correctness; "
            "findings as JSON`, {model: 'fable'})"
        ): Warn(pattern="codex"),
        Input(
            script="if (hasRelevantDiff) { const findings = await agent(`Review the diff for "
            "correctness; findings as JSON`, {model: 'fable'}) }"
        ): Warn(pattern="codex"),
        Input(
            script="const r = await agent(q, { agentType: 'codex:codex-wrapper' })\n"
            "if (!r) await agent('run the codex skill to review the diff', { model: 'sonnet' })"
        ): Warn(pattern="codex-wrapper"),
    },
)

llm_nudge(
    Prompt.load("models/writing_docs_spawn_nudge"),
    message=(
        "Delegated prose points at the `writing-docs` skill instead of paraphrasing its rules. "
        "Rewrite the prompt to tell the agent to read the `writing-docs` skill and its references before it writes."
    ),
    contexts=[ProseSpawn()],
    events=Event.PreToolUse,
    only_if=[Tool("Agent|Task")],
    skip_if=[
        ToolInput("prompt", r"(?i)writing-docs"),
        Agent("Explore|claude-code-guide"),
    ],
    max_context=16_000,
    agent=False,
    transcript=False,
    tests={
        Input(
            prompt="Rewrite the README of /repo. You are fable; technical-builder voice, no hype "
            "adjectives. Verify commands against the binary."
        ): Warn(pattern="writing-docs"),
        Input(
            prompt="Rewrite the README of /repo; technical-builder voice, no hype adjectives. Read "
            "the writing-docs skill at ~/.claude/plugins/cache/skills/writing-docs first."
        ): Allow(),
        Input(prompt="Fix the race in daemon.go; update the failing test"): Allow(),
        Input(
            prompt="Rewrite the README, but read the doc-writing skill and its references first",
            llm={"fire": False},
        ): Allow(),
    },
)

llm_nudge(
    Prompt.load("models/writing_docs_workflow_nudge", workflow_script_header=WORKFLOW_HEADER),
    message=(
        "Workflow prose stages point at the `writing-docs` skill instead of paraphrasing its rules. "
        "Rewrite the `agent()` prompt to tell its subagent to read the `writing-docs` skill and its references."
    ),
    contexts=[ProseWorkflowScript()],
    events=Event.PreToolUse,
    only_if=[Tool("Workflow")],
    skip_if=[WorkflowScript(pattern=r"(?i)writing-docs")],
    max_fires=2,
    max_context=16_000,
    agent=False,
    transcript=False,
    tests={
        Input(
            script="agent('Rewrite the README. You are fable; technical-builder voice, no hype "
            "adjectives', {model: 'opus'})"
        ): Warn(pattern="writing-docs"),
        Input(
            script="agent('Rewrite the README per the writing-docs skill at "
            "~/.claude/plugins/cache/skills/writing-docs', {model: 'opus'})"
        ): Allow(),
        Input(script="agent('Fix the race in daemon.go; update the failing test')"): Allow(),
        Input(
            script="agent('Rewrite the README, but read the doc-writing skill and its references "
            "first', {model: 'opus'})",
            llm={"fire": False},
        ): Allow(),
    },
)
