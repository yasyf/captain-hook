Decide whether this delegated subagent's implementation should route off fable-5
to opus-5, gpt-6.1-sol at xhigh on the standard tier via codex:codex-wrapper, or
sonnet at xhigh for a sweep that must stay Claude-side.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt. A spawn naming no model runs opus, whatever the session model.

The Model Routing rubric: opus-5 is the default implementation lane at every
horizon. Use high for an individual bounded, decision-light change; use xhigh for
ambiguous, exploratory, decision-dense, large net-new, or long-running
implementation. Repetitive bounded N-unit sweeps never run on opus: use sonnet at
xhigh when the lanes must stay Claude-side, and gpt-6.1-sol at xhigh on the
standard tier through codex:codex-wrapper otherwise. Shell-heavy execution also
routes to sol at xhigh. Orchestration, multi-phase autonomous drives,
long-horizon agentic runs, sustained tool-driving, design/architecture review,
hard planning, and synthesis/accept-reject over findings run on opus at xhigh.
Fable-5.1 is reserved for the most sensitive implementation, reached as a typed
model='fable' subagent; ordinary auth, migrations, concurrency, or error-prone
code alone does not qualify. A missed implementation lane crosses between opus
xhigh and sol xhigh before reaching fable. All prose/writing runs on opus, and
code/diff review (finder; refuter only at audit depth), security review/audit,
verification of security-sensitive code, and bug diagnosis have sol xhigh lanes;
separate nudges handle both.

Set fire=true only when the spawn assigns ordinary implementation or a repetitive
bounded sweep to fable without a most-sensitive-implementation need or a
documented escalation after opus xhigh and sol xhigh have fallen short. An
unpinned individual implementation spawn already runs opus: fire=false. Prompts
that review, plan, design, diagnose, or write prose are outside this
implementation nudge: fire=false; judge them by their own lanes. A typed fable
spawn implementing the most sensitive code is routed correctly: fire=false. When
uncertain, fire=false — the agent may have chosen fable deliberately, and a false
alarm teaches it to ignore this nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
model: fable — Implement the pagination endpoint in api/users.py per the spec in the plan.
Specified, decision-light implementation belongs on opus at high.
</example>
<example fire="true">
model: fable — Add a --json flag to the export command and thread it through the formatter.
Decision-light feature wiring belongs on opus at high.
</example>
<example fire="true">
model: fable — Build out the new ingestion subsystem: parser, store, and CLI wiring, shape TBD.
Exploratory, decision-dense implementation belongs on opus at xhigh.
</example>
<example fire="true">
model: fable — Convert the eleven test modules under tests/legacy/ to pytest, one per lane, per the worked example.
A repetitive bounded sweep routes to gpt-6.1-sol at xhigh, or sonnet at xhigh if the lanes must stay Claude-side.
</example>
<example fire="false">
Review the diff for correctness and concurrency issues.
Code/diff finder work routes to gpt-6.1-sol at xhigh through the review nudge.
</example>
<example fire="false">
Design the migration strategy for the sharded session store.
Hard planning and design belong on opus at xhigh; this is outside the implementation nudge.
</example>
<example fire="false">
model: fable — Rotate the production signing keys and re-encrypt the stored secrets without a plaintext window.
Crypto and secret handling at this stake is the most sensitive implementation, assigned to a typed fable subagent.
</example>
</examples>

See CLAUDE.md § Model Routing (§ Plan Execution & Orchestration in repos not yet re-bootstrapped).
