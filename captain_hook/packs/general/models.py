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
    FilePath,
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


@dataclass(frozen=True, slots=True)
class InlineEdit:
    """Gating context: the file the main agent is about to edit inline on the main loop."""

    tag: str = "edit_target"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if not evt.file or evt.content is None:
            return None
        return f"file: {evt.file.path}\nincoming text: {len(evt.content):,} chars"


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
    """Gating context: the pending Workflow call's script source, headed by its model pins."""

    tag: str = "workflow_script"
    required: bool = True

    @staticmethod
    def pins_and_source(evt: BaseHookEvent) -> tuple[str, str] | None:
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
        header = (
            "lines that pin a model in this script (a stage not quoted here inherits the "
            f"session model, fable):\n{pin_lines or '  (none)'}"
        )
        return header, source

    def content(self, evt: BaseHookEvent) -> str | None:
        if (parts := self.pins_and_source(evt)) is None:
            return None
        header, source = parts
        return f"{header}\n\n{source}"


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
than fable and nearly as capable. Fable's lanes are orchestration, design/architecture
review, hard planning, all prose/writing, and implementation that is very sensitive or
error-prone (auth, migrations, concurrency, data loss, crypto, subtle algorithms).
Code/diff review, security review/audit, and bug diagnosis have their own gpt-5.5
lanes with separate nudges.

Set fire=true only when the prompt is clearly routine implementation — building, fixing,
wiring, or refactoring code — with no fable-lane signal. A prompt that reviews, plans,
designs, diagnoses a bug, writes prose, or touches a sensitive surface is not an
implementation prompt: fire=false. When uncertain, fire=false — the agent may have
chosen fable deliberately, and a false alarm teaches it to ignore this nudge. Keep
reasoning under 40 words.

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
Not implementation — review routes via its own nudge (gpt-5.5's lane), not to opus.
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

llm_nudge(
    """Decide whether the main agent should delegate this inline edit instead of making
it itself.

The main loop runs on fable-5; this pending edit is fable implementing directly.
<edit_target> names the file; <before_edit>/<after_edit> hold the text being replaced
and written.

The Models rubric: implementation belongs to a delegated opus-4.8 subagent at xhigh —
~2x cheaper than fable and nearly as capable — or, for a well-scoped edit to existing
code, to gpt-5.5 via the codex skill. Fable edits inline when the change is small or
judgment-bound: a fix-up finishing work it just reasoned through, a subtle algorithm,
or a sensitive surface (auth, migrations, concurrency, data loss, crypto).

Set fire=true only when this edit is clearly substantial routine implementation —
building out a feature, wiring components, refactoring — that a subagent could own end
to end. A small fix-up, a sensitive surface, or a change entangled with judgment the
main agent just exercised stays inline: fire=false. When uncertain, fire=false — the
agent may be editing inline deliberately, and a false alarm teaches it to ignore this
nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
after_edit: a new 180-line pagination module written to src/api/pagination.py.
Substantial net-new feature code — a delegated opus xhigh subagent's lane.
</example>
<example fire="true">
after_edit: rewiring three call sites and adding a formatter class in export.py.
Routine multi-part refactor a subagent could own end to end.
</example>
<example fire="false">
after_edit: a two-line fix to the retry counter the agent just diagnosed.
Small fix-up entangled with judgment already exercised — inline is right.
</example>
<example fire="false">
after_edit: reworking the token-refresh lock in auth/middleware.py.
Auth plus concurrency is a sensitive surface — fable's inline lane.
</example>
</examples>""",
    message=lambda r: (
        f"This inline edit reads as routine implementation on fable. {r.reasoning} "
        "Implementation delegates: spawn a model='opus', effort='xhigh' subagent to own the change, "
        "or route a well-scoped edit to gpt-5.5 via the codex skill. Keep editing inline only when "
        "the change is small, sensitive, or bound to judgment you just exercised. "
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

llm_nudge(
    """Decide whether this delegated subagent runs code review, a security review/audit
or verification of security-sensitive code, or bug diagnosis that should route to
gpt-5.5 instead of fable.

<delegated_spawn> holds the pending Agent/Task call: its model pin (or that it inherits
the session model, fable), agent type, and prompt.

The Models rubric: code/diff review — sweeping a diff or codebase for bugs,
correctness, or cleanups; finder and refuter passes over findings — security
review/audit and verification of security-sensitive code (auth, input validation,
crypto, secrets), and bug diagnosis route to gpt-5.5 via the codex skill; fable is
the escalation target when gpt-5.5's output misses. Fable keeps design/architecture
review, "is this the right approach" judgment, prose review, the synthesis/
accept-reject pass over review findings — and security-sensitive implementation,
which is not review.

Set fire=true only when the prompt clearly reviews code or diffs for defects, audits
or verifies security-sensitive code, or diagnoses a bug, and the spawn would run on
fable. Design review, approach judgment,
synthesis over findings, and prose review are fable's lanes: fire=false. When
uncertain, fire=false — the agent may have chosen fable deliberately, and a false
alarm teaches it to ignore this nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
Review the diff for correctness and concurrency issues; report findings as JSON.
Diff review for defects — gpt-5.5's lane via codex.
</example>
<example fire="true">
Adversarially refute this finding: the retry loop double-increments the counter.
A refuter pass over a code finding is review work.
</example>
<example fire="true">
Diagnose why the exporter hangs when two workers flush concurrently.
Bug diagnosis starts on gpt-5.5; fable is the escalation target.
</example>
<example fire="false">
Judge these three sharding designs and recommend one.
Design/architecture judgment is fable's lane.
</example>
<example fire="false">
Synthesize the confirmed findings and decide which to fix before release.
The accept-reject pass over findings stays on fable.
</example>
<example fire="false">
Review the README draft for factual errors.
Prose review stays on fable.
</example>
<example fire="true">
Audit the session-token handling in auth/middleware.py for vulnerabilities.
Security review/audit of code — gpt-5.5's lane via codex.
</example>
<example fire="true">
Verify the new input-validation layer rejects path traversal and injection payloads.
Verification of security-sensitive code routes to gpt-5.5.
</example>
<example fire="false">
Implement mitigations for the security-audit findings in auth.py.
Security-sensitive implementation, not review — the implementation lanes apply.
</example>
</examples>""",
    message=lambda r: (
        f"This review/diagnosis delegation would run on fable. {r.reasoning} "
        "Code/diff review, security review/audit and verification of security-sensitive code, "
        "and bug diagnosis route to gpt-5.5: run the codex skill (from a "
        "workflow or subagent, spawn a model='sonnet', effort='low' wrapper that writes a "
        "self-contained codex prompt), and escalate to fable only when gpt-5.5's output misses. "
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
        ToolInput("model", r"(?i)\b(opus|sonnet|haiku)\b"),
        Agent("Explore|claude-code-guide"),
    ],
    agent=False,
    transcript=False,
    tests={
        Input(prompt="Review the diff for correctness and concurrency issues"): Warn(pattern="gpt-5.5"),
        Input(model="fable", prompt="Adversarially refute this finding: the retry loop is wrong"): Warn(
            pattern="codex"
        ),
        Input(model="sonnet", prompt="Review the diff for correctness via the codex skill"): Allow(),
        Input(prompt="fix the failing import in cli.py"): Allow(),
        Input(agent_type="Explore", prompt="find where the review pipeline lives"): Allow(),
        Input(
            prompt="Synthesize the confirmed review findings and decide which to fix",
            llm={"fire": False},
        ): Allow(),
        Input(prompt="Audit auth/session.py for security vulnerabilities"): Warn(pattern="gpt-5.5"),
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
    """Decide whether this workflow script runs code review or bug diagnosis stages on
fable that should route to gpt-5.5.

<workflow_script> holds the pending Workflow call's script source, headed by every
line that pins a model — a stage not quoted there carries no pin and inherits the
session model, fable.

The Models rubric: code/diff review stages — finder sweeps over a diff or codebase,
adversarial refuters over findings — security review/audit stages and verification
of security-sensitive code (auth, input validation, crypto, secrets), and bug
diagnosis route to gpt-5.5 via the codex skill. A stage does that correctly when it
pins model 'sonnet' at low effort and its prompt writes a self-contained codex prompt
and runs the codex skill. Fable keeps the synthesis/accept-reject stage over findings
and design/architecture judgment — and security-sensitive implementation, which is
not review.

Set fire=true only when at least one review or diagnosis stage would run on fable —
unpinned, or pinned 'fable'. Stages already wrapped for codex, synthesis stages, and
design judgment are routed right: fire=false. When uncertain, fire=false — a false
alarm teaches the agent to ignore this nudge. Keep reasoning under 40 words and name
the offending stage.

<examples>
<example fire="true">
agent(`Sweep the diff for go-correctness issues; return findings as JSON`)
An unpinned finder inherits fable; finder sweeps are the codex-wrapper lane.
</example>
<example fire="true">
findings.map(f => agent(`Adversarially refute: ${f.title}`, {effort: 'max'}))
Refuters over code findings inherit fable — route them through codex wrappers.
</example>
<example fire="false">
agent(`Write a self-contained codex prompt reviewing this diff for correctness,
then run the codex skill`, {model: 'sonnet', effort: 'low'})
Already the codex wrapper — correctly routed.
</example>
<example fire="false">
agent(`Synthesize the confirmed findings and decide which to fix`)
Synthesis/accept-reject stays on fable.
</example>
<example fire="true">
agent(`Audit the auth flow for injection and session-fixation issues; return findings as JSON`)
An unpinned security audit inherits fable; security review/audit is the codex-wrapper lane.
</example>
</examples>""",
    message=lambda r: (
        f"This workflow runs review/diagnosis stages on fable. {r.reasoning} "
        "Route finder, refuter, security-audit, and diagnosis stages to gpt-5.5: make each a model: 'sonnet', "
        "effort: 'low' stage that writes a self-contained codex prompt and runs the codex skill; "
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
            pattern="gpt-5.5"
        ),
        Input(
            script="agent('Write a self-contained codex prompt reviewing this diff, "
            "then run the codex skill', {model: 'sonnet', effort: 'low'})",
            llm={"fire": False},
        ): Allow(),
        Input(script="agent('fix the failing import in cli.py')"): Allow(),
        Input(
            script="agent(`Synthesize the confirmed review findings and decide which to fix`)",
            llm={"fire": False},
        ): Allow(),
        Input(script="agent(`Audit the login flow for auth bypass and injection; return findings as JSON`)"): Warn(
            pattern="gpt-5.5"
        ),
        Input(script="agent('Verify the CLI renders the last page correctly')"): Allow(),
    },
)

llm_nudge(
    """Decide whether this delegated subagent call delegates documentation or prose
writing without directing the subagent to read the writing-docs skill.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing verb
of a prose artifact, with negated asks ("do NOT edit the docs") already screened out.

You are watching an orchestrating agent spawn a subagent. Decide whether the pending
prompt delegates documentation or prose writing — a README, docs page, CHANGELOG,
tutorial, release notes, or similar deliverable — without directing the subagent to
read the writing-docs skill. Restated style rules ('technical-builder voice', 'no hype
adjectives', 'first person, confident') do not count as reading the skill; that
paraphrase is exactly the failure to catch. Fire only when the prompt's deliverable is
prose the writing-docs skill governs. Do not fire for code work that incidentally
mentions a doc file, for reading or reviewing docs without writing them, or for a prompt
that already tells the agent to read the skill or its references.

When uncertain, fire=false — a false alarm teaches the agent to ignore this nudge. Keep
reasoning under 40 words.

<examples>
<example fire="true">
Rewrite the README for this repo. You are fable; technical-builder voice, no hype adjectives.
README prose with the style rules paraphrased in place of the skill — the drift this catches.
</example>
<example fire="true">
Draft the release notes for v2. Keep it first-person and confident, no marketing fluff.
Release-notes prose; restated voice rules, no pointer to the writing-docs skill.
</example>
<example fire="false">
Fix the failing test in cli.py; the README already documents the new flag.
Code work that only mentions the README — no prose is produced.
</example>
<example fire="false">
Rewrite the README, but read the doc-writing skill and its references first.
Already directs the subagent to the skill — nothing to nudge.
</example>
</examples>""",
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
    """Decide whether this workflow script delegates documentation or prose writing to an
agent() stage without directing that subagent to read the writing-docs skill.

<workflow_script> holds the pending Workflow call's script source, headed by every line
that pins a model, followed by the sentences a clause prefilter matched — each asks a
writing verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md") already
screened out.

Decide whether an agent() prompt delegates documentation or prose writing — a README,
docs page, CHANGELOG, tutorial, release notes, or similar deliverable — without
directing that subagent to read the writing-docs skill. Restated style rules
('technical-builder voice', 'no hype adjectives', 'first person, confident') do not
count as reading the skill; that paraphrase is exactly the failure to catch. Fire only
when a stage's deliverable is prose the writing-docs skill governs. Do not fire for code
work that incidentally mentions a doc file, for reading or reviewing docs without writing
them, or for a stage that already tells its subagent to read the skill or its references.

When uncertain, fire=false — a false alarm teaches the agent to ignore this nudge. Keep
reasoning under 40 words and name the offending stage.

<examples>
<example fire="true">
agent('Rewrite the README for the new CLI. Technical-builder voice, no hype adjectives', {model: 'opus'})
The stage's deliverable is README prose with the style rules paraphrased in place of the skill.
</example>
<example fire="true">
agent(`Draft the docs-site page for ${feature}. First-person, confident, no marketing fluff`)
A docs page delegated with restated voice rules and no pointer to the writing-docs skill.
</example>
<example fire="false">
agent('Fix the failing import in cli.py; the README already documents the flag', {model: 'opus'})
Code work that only mentions the README — no prose is produced.
</example>
<example fire="false">
agent('Rewrite the troubleshooting guide, but read the doc-writing skill and its references first')
The stage already directs its subagent to the skill — nothing to nudge.
</example>
</examples>""",
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
