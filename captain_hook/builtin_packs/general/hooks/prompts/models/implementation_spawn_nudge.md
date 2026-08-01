Decide whether this delegated subagent's work should route off fable-5 — to opus-5, or to gpt-5.6-sol via the codex:codex-wrapper agent.

<delegated_spawn> holds the pending Agent/Task call: its model pin (or that it inherits
the session model, fable), agent type, and prompt.

The Models rubric: implementation delegates off fable, and the default lane is opus-5
(~2x cheaper than fable and nearly as capable) — at high effort when the work is bounded
and decision-light (the plan, work order, or repeated pattern already made the decisions),
at xhigh when the implementation is ambiguous, exploratory, decision-dense, or a
long-running build. Only a repetitive N-unit sweep (migrations, test conversions,
mechanical refactors as parallel lanes) or
terminal/shell-heavy execution goes to gpt-5.6-sol via the codex:codex-wrapper agent —
sol's per-task token efficiency pays only multiplied across a fan-out. Fable's lanes are
orchestration, design/architecture review, hard planning, all prose/writing, long-horizon
agentic driving and sustained tool-driving (browser automation, QA sweeps), and
implementation that is very sensitive or error-prone (auth, migrations, concurrency,
data loss, crypto, subtle algorithms). Code/diff review, security review/audit, and bug
diagnosis have their own gpt-5.6-sol lanes with separate nudges.

Set fire=true only when the prompt is clearly routine implementation — building, fixing,
wiring, or refactoring code — with no fable-lane signal. A prompt that reviews, plans,
designs, diagnoses a bug, writes prose, or touches a sensitive surface is not an
implementation prompt: fire=false. When uncertain, fire=false — the agent may have
chosen fable deliberately, and a false alarm teaches it to ignore this nudge. Keep
reasoning under 40 words.

<examples>
<example fire="true">
Implement the pagination endpoint in api/users.py per the spec in the plan.
Spec'd, decision-light implementation — the opus high lane; off fable either way.
</example>
<example fire="true">
Add a --json flag to the export command and thread it through the formatter.
Decision-light feature wiring — the opus high lane.
</example>
<example fire="true">
Build out the new ingestion subsystem: parser, store, and CLI wiring, shape TBD.
Exploratory, decision-dense implementation — the opus xhigh lane.
</example>
<example fire="true">
Convert the eleven test modules under tests/legacy/ to pytest, one per lane, per the worked example.
Repetitive N-unit sweep — the gpt-5.6-sol fan-out lane.
</example>
<example fire="false">
Review the diff for correctness and concurrency issues.
Not implementation — review routes via its own nudge (gpt-5.6-sol's lane), not to opus.
</example>
<example fire="false">
Design the migration strategy for the sharded session store.
Hard planning/design stays on fable.
</example>
<example fire="false">
Implement the token-refresh race fix in the auth middleware.
Auth plus concurrency: sensitive, error-prone implementation stays on fable.
</example>
</examples>
