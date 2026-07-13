Decide whether this delegated subagent's work should route off fable-5 — to opus-4.8, or to gpt-5.6-sol via the codex:codex-wrapper agent.

<delegated_spawn> holds the pending Agent/Task call: its model pin (or that it inherits
the session model, fable), agent type, and prompt.

The Models rubric: implementation delegates off fable — ambiguous or exploratory work,
large multi-file refactors, and long agentic runs go to opus-4.8 at xhigh (~2x cheaper
than fable and nearly as capable); well-scoped, clearly-bounded
implementation (terminal-heavy included) goes to gpt-5.6-sol via the codex:codex-wrapper agent. Fable's lanes are
orchestration, design/architecture review, hard planning, all prose/writing, and
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
Spec'd, clearly-bounded implementation — the gpt-5.6-sol lane; off fable either way.
</example>
<example fire="true">
Add a --json flag to the export command and thread it through the formatter.
Well-scoped feature wiring — the gpt-5.6-sol lane.
</example>
<example fire="true">
Build out the new ingestion subsystem: parser, store, and CLI wiring, shape TBD.
Exploratory multi-file implementation — the opus xhigh lane.
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
