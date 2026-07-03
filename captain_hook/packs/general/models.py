from __future__ import annotations

import re
from dataclasses import dataclass

from captain_hook import (
    Agent,
    Allow,
    BaseHookEvent,
    Block,
    Clause,
    Event,
    Input,
    Phrase,
    Rewrite,
    TaskCall,
    Tool,
    ToolInput,
    Warn,
    WorkflowScript,
    hook,
    llm_gate,
    llm_nudge,
    nudge,
    set_tool_input,
    workflow_opt_values,
    workflow_script_source,
)
from captain_hook.signals.nlp import nlp_scan


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


WORKFLOW_SCRIPT_CAP = 14_000  # below the prose hooks' max_context=16_000, so truncation stays ours


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
class WorkflowScriptSource:
    """Gating context: the pending Workflow call's script source, headed by its model pins and prose asks."""

    tag: str = "workflow_script"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if (source := workflow_script_source(evt)) is None:
            return None
        pin_lines = "\n".join(
            f"  {line.strip()[:200]}" for line in source.splitlines() if workflow_opt_values(line, "model")
        )
        if len(source) > WORKFLOW_SCRIPT_CAP:
            head, tail = WORKFLOW_SCRIPT_CAP * 3 // 4, WORKFLOW_SCRIPT_CAP // 4
            source = (
                f"{source[:head]}\n"
                f"… [script truncated: {len(source):,} chars total; every model pin is quoted above] …\n"
                f"{source[-tail:]}"
            )
        if not (sentences := prose_deliverable_sentences(source)):
            return None
        matched = "\n".join(f"  {s[:300]}" for s in sentences)
        return (
            "lines that pin a model in this script (a stage not quoted here inherits the "
            f"session model, fable):\n{pin_lines or '  (none)'}\n\n"
            f"sentences the prose prefilter matched:\n{matched}\n\n{source}"
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

llm_gate(
    """Decide whether this delegated subagent call pins a non-fable model on work whose
deliverable is prose.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing
verb of a prose artifact, with negated asks ("do NOT edit the docs") already
screened out. Your job is precision: is a prose artifact what this subagent is
asked to PRODUCE?

The Models rubric: all writing a user reads — READMEs, docs, changelogs, release
notes, blog posts, PR descriptions, any user-facing text — routes to fable. Work
that merely mentions a prose file — as a constraint ("do NOT touch the docs"), as
reading material, as the subject of recon or review — is not prose work.

Set block=true only when the prompt clearly asks the subagent to write, draft,
revise, or polish a prose artifact. Recon, review, classification, and code work
that references docs stay allowed: block=false. When uncertain, block=false — a
wrong block stops legitimate work cold. Keep reasoning under 40 words.

<examples>
<example block="true">
model: sonnet — Write the README quickstart for this repo.
The deliverable is README prose on a non-fable pin.
</example>
<example block="true">
model: opus — Update CHANGELOG.md with an entry for the retry fix.
The subagent itself writes the changelog prose.
</example>
<example block="false">
model: opus — Fix the failing test in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it.
CHANGELOG is a constraint, not the deliverable; this is code work.
</example>
<example block="false">
model: sonnet — Explore the cc-interact building blocks and report where the docs pipeline is written.
Read-only recon that mentions docs; nothing user-facing is produced.
</example>
<example block="false">
model: sonnet — Review the README draft for factual errors and list them.
Review findings go to the orchestrator; the subagent writes no user-facing prose.
</example>
</examples>""",
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

llm_nudge(
    """Decide whether this workflow script pins a non-fable model on a stage whose
deliverable is prose.

<workflow_script> holds the pending Workflow call's script source. Its header quotes
every line of the script that pins a model — a stage that is not quoted there carries
no pin and inherits the session model, fable — already correctly routed, whatever it
writes — followed by the sentences a clause prefilter matched: each asks a writing
verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md") already
screened out. Your job is precision: does a PINNED stage have prose as its own
deliverable?

The Models rubric: all writing a user reads — READMEs, docs, changelogs, release
notes, blog posts, announcements, any user-facing text — routes to fable. A stage's
deliverable is prose when its agent() prompt asks it to write, draft, revise, or
polish such an artifact.

Set fire=true only when at least one agent() call both pins haiku/sonnet/opus and
has prose as its deliverable. A prose stage with no pin of its own is fine, even
when other stages pin opus. A prose keyword that appears as a constraint ("do NOT
edit CHANGELOG.md"), an ownership note, a file the stage merely reads, a
meta.description, or prose the orchestrator script assembles itself outside any
pinned agent() call is not that stage's deliverable: fire=false. When uncertain,
fire=false — a false alarm teaches the agent to ignore this nudge. Keep reasoning
under 40 words and name the offending stage.

<examples>
<example fire="true">
agent('Write the README quickstart section for the new CLI', {model: 'opus'})
The stage's deliverable is README prose, pinned to opus.
</example>
<example fire="true">
stages.push(agent(`Draft the docs-site page for ${feature}`, {model: 'sonnet'}))
A docs page is user-facing prose; the pin is non-fable.
</example>
<example fire="false">
agent('Fix the failing import in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'opus'})
CHANGELOG appears only as a constraint; the deliverable is a code fix.
</example>
<example fire="false">
agent('Fix the CLI error handling', {label: 'fix:cli', model: 'opus'}) alongside
agent('Reword the troubleshooting guide and CHANGELOG bullet', {label: 'fix:docs'})
The prose stage carries no pin — it inherits fable; the opus pin is on code.
</example>
<example fire="false">
meta: {description: 'verify the doc claims against actual behavior'}, then agent('run the test matrix', {model: 'opus'})
"doc claims" lives in the description, not in any pinned stage's deliverable.
</example>
<example fire="false">
agent('fix the three failing tests', {model: 'opus'}), then the script itself assembles CHANGELOG.md from the results
The orchestrator writes the prose; the pinned stage only fixes tests.
</example>
<example fire="false">
agent('Read docs/architecture.md and list the sections that are stale', {model: 'sonnet'})
Reading and classifying docs is analysis, not a prose deliverable.
</example>
</examples>""",
    message=lambda r: (
        f"This workflow script pins a non-fable model on a stage whose deliverable is prose. {r.reasoning} "
        "All writing — docs, READMEs, release notes, any user-facing text — routes to fable: inherit the "
        "session model or pin model: 'fable' on that stage. "
        "See CLAUDE.md § Plan Execution & Orchestration (Models)."
    ),
    contexts=[WorkflowScriptSource()],
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
