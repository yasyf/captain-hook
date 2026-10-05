Decide whether this workflow script runs code/diff review, security review/audit,
verification of security-sensitive code, or bug diagnosis stages outside their
gpt-6.1-sol xhigh lane.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}

The Model Routing rubric: code/diff review is sol's finder lane, with a refuter
only at audit depth. Security review/audit, verification of security-sensitive
code (auth, input validation, crypto, secrets), and bug diagnosis also route to
gpt-6.1-sol at xhigh on the standard tier. Give each stage agentType:
'codex:codex-wrapper' and a self-contained question as its prompt, including the
files or diff to inspect. A Claude-model stage that asks its agent to run the
codex skill is the retired wrapper shape. Design/architecture review and
synthesis/accept-reject over findings run on opus at xhigh. An unpinned stage
runs opus; it never inherits the session model. A sol miss permits escalation to
opus at xhigh. Fable is never a review or diagnosis lane.

{deliverable_rubric}

Set fire=true when a sol-lane review or diagnosis stage runs on a Claude model
before a sol attempt for the same work, or runs on fable. A stage that asks a
Claude model to run the codex skill itself stays fire=true, including in a
fallback branch. Stages routed through codex:codex-wrapper, and design or
synthesis stages on opus at xhigh, are routed correctly: fire=false. A synthesis
or accept-reject stage is never a finding on any route. An opus xhigh review or
diagnosis stage reached only after a sol stage for the same work returns nothing
is an allowed escalation: fire=false. A feature flag or input check does not
establish an escalation; trace whether the review call runs before, instead of,
or only after the sol attempt. Accept a stated prior failure in meta or comments
for the work it names, even when that attempt is outside the script: "sol lane
quota-dead; opus xhigh escalation" clears an opus stage. Judge unrelated stages
separately. When uncertain, fire=false — a false alarm teaches the agent to
ignore this nudge. Keep reasoning under 40 words and name the offending stage.

<examples>
<example fire="true">
agent(`Sweep the diff for go-correctness issues; return findings as JSON`)
An unpinned finder runs opus; use gpt-6.1-sol at xhigh via codex:codex-wrapper.
</example>
<example fire="true">
findings.map(f => agent(`At audit depth, adversarially refute: ${f.title}`, {effort: 'xhigh'}))
Audit-depth refuters belong on gpt-6.1-sol at xhigh via codex:codex-wrapper.
</example>
<example fire="true">
agent('Write a self-contained codex prompt reviewing this diff, then run the codex skill', {model: 'sonnet', effort: 'low'})
The retired wrapper shape: use agentType: 'codex:codex-wrapper' to reach sol at xhigh.
</example>
<example fire="false">
agent(`Review the diff hunks in src/ for correctness; return findings as JSON`, {agentType: 'codex:codex-wrapper', effort: 'xhigh'})
The finder routes through codex:codex-wrapper to gpt-6.1-sol at xhigh.
</example>
<example fire="false">
agent(`Synthesize the confirmed findings and decide which to fix`, {model: 'opus', effort: 'xhigh'})
Synthesis/accept-reject runs on opus at xhigh.
</example>
<example fire="false">
agent(`Synthesize the confirmed findings and decide which to fix`, {agentType: 'codex:codex-wrapper', effort: 'xhigh'})
Synthesis/accept-reject is never a finding on any route.
</example>
<example fire="false">
const r = await agent(q, { agentType: 'codex:codex-wrapper', effort: 'xhigh', phase: 'Review', schema: REVIEW })
if (r) return r
log('sol empty — opus fallback')
return await agent(q, { effort: 'xhigh', phase: 'Review', schema: REVIEW })
The unpinned Review call runs opus at xhigh only after sol returns nothing; this is the allowed escalation.
</example>
<example fire="false">
export const meta = { name: 'p1-review', description: 'Opus finder+refuter at audit depth over the landed P1 commit (sol lane quota-dead; opus xhigh escalation per models table)', phases: [{ title: 'Review' }] }
const f = await agent(`Review the landed diff for correctness; findings as JSON`, { effort: 'xhigh', label: 'find:opus', phase: 'Review', schema: REVIEW })
The meta states a prior sol failure; the unpinned stage runs opus at xhigh for that work.
</example>
<example fire="true">
const findings = await agent(`Review the diff in src/ for correctness; findings as JSON`, {model: 'fable'})
Fable is never a review lane; route the finder to gpt-6.1-sol at xhigh via codex:codex-wrapper.
</example>
<example fire="true">
agent(`Audit the auth flow for injection and session-fixation issues; return findings as JSON`)
An unpinned audit runs opus; security review/audit belongs on gpt-6.1-sol at xhigh via codex:codex-wrapper.
</example>
</examples>

See CLAUDE.md § Model Routing (§ Plan Execution & Orchestration in repos not yet re-bootstrapped).
