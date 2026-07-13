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
    Event,
    FilePath,
    Input,
    Not,
    Or,
    Phrase,
    Prompt,
    Rewrite,
    TaskCall,
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
from captain_hook.signals.nlp import nlp_scan

DELIVERABLE_GATE_RUBRIC = str(Prompt.load("fragments/deliverable_rubric", verdict_attr="block"))
DELIVERABLE_NUDGE_RUBRIC = str(Prompt.load("fragments/deliverable_rubric", verdict_attr="fire"))
WORKFLOW_HEADER = str(Prompt.load("fragments/workflow_script_header"))

PROSE_SPAWN_GATE = Prompt.load("models/prose_spawn_gate", deliverable_rubric=DELIVERABLE_GATE_RUBRIC)
IMPLEMENTATION_SPAWN_NUDGE = Prompt.load("models/implementation_spawn_nudge")
INLINE_EDIT_NUDGE = Prompt.load("models/inline_edit_nudge")
BROWSER_DELEGATION_NUDGE = Prompt.load("models/browser_delegation_nudge")
REVIEW_ROUTING_SPAWN_NUDGE = Prompt.load("models/review_routing_spawn_nudge")
PROSE_WORKFLOW_NUDGE = Prompt.load(
    "models/prose_workflow_nudge",
    workflow_script_header=WORKFLOW_HEADER,
    deliverable_rubric=DELIVERABLE_NUDGE_RUBRIC,
)
REVIEW_ROUTING_WORKFLOW_NUDGE = Prompt.load(
    "models/review_routing_workflow_nudge",
    workflow_script_header=WORKFLOW_HEADER,
    deliverable_rubric=DELIVERABLE_NUDGE_RUBRIC,
)
WRITING_DOCS_SPAWN_NUDGE = Prompt.load("models/writing_docs_spawn_nudge")
WRITING_DOCS_WORKFLOW_NUDGE = Prompt.load("models/writing_docs_workflow_nudge", workflow_script_header=WORKFLOW_HEADER)


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


@dataclass(frozen=True, slots=True)
class InlineEdit:
    """Gating context: the file the main agent is about to edit inline on the main loop."""

    tag: str = "edit_target"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if not evt.file or evt.content is None:
            return None
        return f"file: {evt.file.path}\nincoming text: {len(evt.content):,} chars"


def prose_deliverable_sentences(text: str) -> list[str]:
    """Sentences where a writing verb governs a prose artifact, minus negated asks ("do NOT edit CHANGELOG.md").

    The text is de-noised for the tagger first: path/URL tokens dropped, brackets and
    word-edge quotes blanked (so `agent('write …')` doesn't glue into one token; intra-word
    apostrophes survive for "n't" negations), readme/changelog extensions stripped, intra-word
    hyphens split, writing verbs lowercased, and imperative writing verbs given a determiner —
    "Update CHANGELOG.md" otherwise parses as a noun compound.
    """
    verbs = r"write|draft|redraft|rewrite|revise|reword|polish|copyedit|compose|author|update|edit"
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
    writing = Phrase(
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
    if not (matched := nlp_scan([Clause(noun=artifact, verb=writing)], text)):
        return []
    negated = set(nlp_scan([Clause(noun=artifact, verb=writing, negated=True)], text))
    return [s for s in matched if s not in negated]


@dataclass(frozen=True, slots=True)
class ProseSpawn(DelegatedSpawn):
    """Gating context: the pending spawn, present only when its prompt asks to produce a prose artifact."""

    def content(self, evt: BaseHookEvent) -> str | None:
        # zero-arg super() breaks under @dataclass(slots=True) — the decorator rebuilds the class
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

llm_gate(
    PROSE_SPAWN_GATE,
    message=lambda r: (
        "This subagent is pinned to a non-fable model but its deliverable is prose/writing work. "
        f"{r.reasoning} All writing — docs, READMEs, release notes, any user-facing text — routes "
        "to fable: drop model to inherit the session model, or pass model='fable'. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[ProseSpawn()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Agent|Task"),
        ToolInput("model", r"(?i)\b(haiku|sonnet|opus)\b"),
    ],
    skip_if=[
        ToolInput("prompt", r"(?i)\b(classif|label|tag|categoriz|count|extract|mechanical)"),
        Agent("Explore|claude-code-guide"),
    ],
    agent=False,
    transcript=False,
    max_context=16_000,
    tests={
        Input(model="sonnet", prompt="Write the README quickstart for this repo"): Block(),
        Input(model="opus", prompt="draft the release notes for v2"): Block(),
        Input(model="haiku", prompt="update the CHANGELOG entry for the fix"): Block(),
        Input(model="fable", prompt="write the README quickstart"): Allow(),
        Input(prompt="write the README quickstart"): Allow(),
        Input(model="sonnet", prompt="review the README for factual errors"): Allow(),
        Input(model="sonnet", prompt="update the retry backoff config"): Allow(),
        Input(model="haiku", prompt="label each README section with its Diataxis mode"): Allow(),
        Input(agent_type="Explore", model="sonnet", prompt="find where the README quickstart is written"): Allow(),
        Input(agent_type="claude-code-guide", model="sonnet", prompt="explain how the docs get updated"): Allow(),
        Input(model="opus", prompt="Update the retry backoff config per the spec in docs/plan.md"): Allow(),
        Input(
            model="opus",
            prompt="Fix the failing test in cli.py. Do NOT edit the CHANGELOG — a sibling owns updating it",
        ): Allow(),
        Input(
            model="opus",
            prompt="Fix the failing test in cli.py; do NOT edit CHANGELOG.md. Then draft the release notes.",
            llm={"block": False},
        ): Allow(),
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
    IMPLEMENTATION_SPAWN_NUDGE,
    message=lambda r: (
        f"This delegation would run on fable, but it reads as routine implementation. {r.reasoning} "
        "Ambiguous, large-refactor, or long-run implementation defaults to model='opus' + effort='xhigh' "
        "(~2x cheaper, nearly as capable); well-scoped, clearly-bounded, or terminal-heavy implementation "
        "routes to gpt-5.6-sol: spawn the codex:codex-wrapper agent with a self-contained prompt. "
        "Keep fable if this genuinely is sensitive or error-prone. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
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
        Input(prompt="add a retry wrapper around the upload call in api/files.py"): Warn(pattern="gpt-5.6"),
        Input(model="opus", prompt="implement the pagination endpoint in api/users.py"): Allow(),
        Input(model="sonnet", prompt="scan the repo for TODO markers"): Allow(),
        Input(agent_type="Explore", prompt="find where the config loader lives"): Allow(),
        Input(agent_type="codex:codex-wrapper", prompt="Apply the edit described here to utils/backoff.py"): Allow(),
    },
)

llm_nudge(
    INLINE_EDIT_NUDGE,
    message=lambda r: (
        f"This inline edit reads as routine implementation on fable. {r.reasoning} "
        "Implementation delegates: a well-scoped, clearly-bounded change routes to gpt-5.6-sol via the "
        "codex skill; ambiguous, open-ended, or long-running work goes to a model='opus', effort='xhigh' subagent. Keep "
        "editing inline only when the change is small, sensitive, or bound to judgment you just exercised. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[InlineEdit()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Edit|Write|MultiEdit"),
        FilePath(
            "**/*.py",
            "**/*.go",
            "**/*.swift",
            "**/*.rs",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.rb",
            "**/*.java",
            "**/*.kt",
            "**/*.c",
            "**/*.cc",
            "**/*.cpp",
            "**/*.h",
            "**/*.zig",
        ),
    ],
    skip_if=[
        FilePath("**/test_*.py", "**/*_test.py", "**/*_test.go", "**/tests/**", "**/*.test.*", "**/*.spec.*"),
    ],
    when=lambda evt: not evt.is_subagent and len(evt.content or "") >= 400,
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
        ): Warn(pattern="gpt-5.6"),
        Input(
            file="README.md",
            content="Pagination lands in the users API.\n" * 20,
        ): Allow(),
        Input(file="src/api/users.py", old="page = 1", content="page = 2"): Allow(),
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


def browser_calls(n: int, *, tool: str = "Bash", field: str = "command", value: str = "agent-browser click '#next'"):
    """A same-turn run of ``n`` browser tool_use lines (Bash by default) for the delegation-nudge inline tests."""
    calls = [{"type": "tool_use", "name": tool, "input": {field: value}, "id": f"tu-{tool}-{i}"} for i in range(n)]
    return [{"type": "assistant", "message": {"content": [call]}} for call in calls]


llm_nudge(
    BROWSER_DELEGATION_NUDGE,
    message=lambda r: (
        f"This is sustained browser automation running inline on fable. {r.reasoning} "
        "Hands-on browser work delegates like any implementation: spawn a model='opus', effort='xhigh' "
        "subagent to drive agent-browser and return findings, or an agent-browser-with-cookies teammate "
        "when the site needs your login. Keep driving the browser inline only for a single gated, stateful, "
        "or authenticated interaction you just decided to run (a go/no-go verification). "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    events=Event.PostToolUse,
    only_if=[
        Tool("Bash|Skill"),
        Or(
            ToolInput("command", r"(?i)\b(agent-browser|playwright)\b"),
            ToolInput("skill", r"(?i)\b(agent-browser|playwright)\b"),
        ),
    ],
    when=lambda evt: (
        not evt.is_subagent
        and (
            evt.ctx.t.current_turn.tool_calls.named("Bash")
            .where_input(command=re.compile(r"(?i)\b(agent-browser|playwright)\b"))
            .count()
            + evt.ctx.t.current_turn.tool_calls.named("Skill")
            .where_input(skill=re.compile(r"(?i)\b(agent-browser|playwright)\b"))
            .count()
        )
        >= 5
    ),
    max_fires=1,
    agent=False,
    transcript=True,
    tests={
        Input(command="agent-browser click '#submit'", transcript=browser_calls(5)): Warn(pattern="opus"),
        Input(command="npx agent-browser click '#next'", transcript=browser_calls(5)): Warn(pattern="opus"),
        Input(command="playwright-cli click e15", transcript=browser_calls(5)): Warn(pattern="opus"),
        Input(
            tool="Skill",
            tool_input={"skill": "agent-browser-with-cookies"},
            transcript=browser_calls(5),
        ): Warn(pattern="opus"),
        Input(
            command="agent-browser click '#submit'",
            transcript=browser_calls(3) + browser_calls(2, tool="Skill", field="skill", value="agent-browser"),
        ): Warn(pattern="opus"),
        Input(command="agent-browser click '#submit'", agent_id="tm1", transcript=browser_calls(5)): Allow(),
        Input(command="agent-browser screenshot out.png"): Allow(),
        Input(command="agent-browser click '#submit'", transcript=browser_calls(5), llm={"fire": False}): Allow(),
        Input(command="ls -la", transcript=browser_calls(5)): Allow(),
    },
)

llm_nudge(
    REVIEW_ROUTING_SPAWN_NUDGE,
    message=lambda r: (
        f"This review/diagnosis delegation would run on fable. {r.reasoning} "
        "Code/diff review, security review/audit and verification of security-sensitive code, "
        "and bug diagnosis route to gpt-5.6-sol: spawn the codex:codex-wrapper agent with the "
        "self-contained question as its prompt (from the main conversation, run the codex skill "
        "directly), and escalate to fable only when gpt-5.6-sol's output misses. "
        "Design review and findings synthesis stay on fable. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[DelegatedSpawn()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Agent|Task"),
        ToolInput(
            "prompt",
            r"(?i)(\b(review|refut|adversari|audit|correctness|diagnos|root.?caus|secur|vuln|pentest)"
            r"|\bverif\w*[\s\S]{0,160}?\b(auth|crypt|secret|sanitiz|inject|input.?valid|token|session))",
        ),
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
        Input(prompt="Review the diff for correctness and concurrency issues"): Warn(pattern="gpt-5.6"),
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
        Input(prompt="Audit auth/session.py for security vulnerabilities"): Warn(pattern="gpt-5.6"),
        Input(prompt="Verify the input-validation change blocks path traversal"): Warn(pattern="codex"),
        Input(prompt="Verify the pagination change renders the last page correctly"): Allow(),
        Input(
            prompt="Implement mitigations for the security audit findings in auth.py",
            llm={"fire": False},
        ): Allow(),
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

llm_nudge(
    PROSE_WORKFLOW_NUDGE,
    message=lambda r: (
        f"This workflow script pins a non-fable model on a stage whose deliverable is prose. {r.reasoning} "
        "All writing — docs, READMEs, release notes, any user-facing text — routes to fable: inherit the "
        "session model or pin model: 'fable' on that stage. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[ProseWorkflowScript()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Workflow"),
        WorkflowScript(model="haiku|sonnet|opus"),
    ],
    max_fires=2,
    max_context=16_000,
    agent=False,
    transcript=False,
    tests={
        Input(script="steps:\n  - agent: write the README intro\n    model: 'sonnet'\n"): Warn(pattern="fable"),
        Input(script="steps:\n  - agent: write the README intro\n    model: 'fable'\n"): Allow(),
        Input(script="steps:\n  - agent: fix the retry backoff\n    model: 'sonnet'\n"): Allow(),
        Input(script="agent('Audit docs/architecture.md for stale claims', {model: 'opus'})"): Allow(),
        Input(
            script="agent('recon the module map', {model: 'sonnet'})\n// all prose stays with the main agent on fable\n"
        ): Allow(),
        Input(
            script="agent('Fix the import in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'opus'})",
        ): Allow(),
    },
)

llm_nudge(
    REVIEW_ROUTING_WORKFLOW_NUDGE,
    message=lambda r: (
        f"This workflow runs review/diagnosis stages on fable. {r.reasoning} "
        "Route finder, refuter, security-audit, and diagnosis stages to gpt-5.6-sol: give each stage "
        "agentType: 'codex:codex-wrapper' with the self-contained question as its prompt; "
        "keep the synthesis/accept-reject stage on fable (inherit the session model). "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[WorkflowScriptSource()],
    events=Event.PreToolUse,
    only_if=[
        Tool("Workflow"),
        WorkflowScript(
            pattern=r"(?i)(\b(review|refut|adversari|audit|correctness|diagnos|root.?caus|secur|vuln|pentest)"
            r"|\bverif\w*[\s\S]{0,160}?\b(auth|crypt|secret|sanitiz|inject|input.?valid|token|session))",
        ),
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
            pattern="gpt-5.6"
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
        Input(script="agent(`Audit the login flow for auth bypass and injection; return findings as JSON`)"): Warn(
            pattern="gpt-5.6"
        ),
        Input(script="agent('Verify the CLI renders the last page correctly')"): Allow(),
    },
)

llm_nudge(
    WRITING_DOCS_SPAWN_NUDGE,
    message=lambda r: (
        "This prompt delegates prose but paraphrases the writing rules instead of pointing at them. "
        f"{r.reasoning} A paraphrase drifts and silently overrides the skill — rewrite the prompt to "
        "direct the agent to READ the writing-docs skill and its references (the installed plugin under "
        "~/.claude/plugins/cache/skills/writing-docs, or plugins/writing-docs in the cc-skills repo) "
        "before it writes."
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
    WRITING_DOCS_WORKFLOW_NUDGE,
    message=lambda r: (
        "This workflow script delegates prose but paraphrases the writing rules instead of pointing at "
        f"them. {r.reasoning} A paraphrase drifts and silently overrides the skill — rewrite the offending "
        "agent() prompt to direct its subagent to READ the writing-docs skill and its references (the "
        "installed plugin under ~/.claude/plugins/cache/skills/writing-docs, or plugins/writing-docs in "
        "the cc-skills repo) before it writes."
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
