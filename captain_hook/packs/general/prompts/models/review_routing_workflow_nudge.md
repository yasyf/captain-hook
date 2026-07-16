Decide whether this workflow script runs code review or bug diagnosis stages on
fable that should route to gpt-5.6-sol.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}

The Models rubric: code/diff review stages — finder sweeps over a diff or codebase,
adversarial refuters over findings — security review/audit stages and verification
of security-sensitive code (auth, input validation, crypto, secrets), and bug
diagnosis route to gpt-5.6-sol via the codex-wrapper agent. A stage does that correctly
when its agent() call pins agentType 'codex:codex-wrapper' and its prompt is the
self-contained question (or pointers to the files/diff to gather plus the questions
to answer). A stage that pins a Claude model and asks its agent to run the codex
skill is the retired wrapper shape — it is NOT routed. Fable is the escalation
target when gpt-5.6-sol's output misses: a Claude-model stage that runs only after
a codex-wrapper stage for the same work returns nothing is the sanctioned
fallback, not misrouting. Fable keeps the
synthesis/accept-reject stage over findings and design/architecture judgment — and
security-sensitive implementation, which is not review.

{deliverable_rubric}

Set fire=true when at least one review or diagnosis stage would run on a Claude
model: unpinned (inherits fable), pinned 'fable', or pinned to any model with a
prompt that runs the codex skill itself — the retired wrapper stays fire=true
wherever it appears, fallback branches included. Stages routed via agentType
'codex:codex-wrapper', synthesis stages, and design judgment are routed right:
fire=false. So is an escalation fallback: a Claude-model review or diagnosis stage
whose code path is reached only when a codex-wrapper stage for the same work
returns nothing (an empty result, a miss). A stage gated on anything else — a
feature flag, an input check — is not a fallback: judge reachability by tracing
whether the Claude-model call runs before or instead of the codex-wrapper attempt,
or only after it fails. Before you fire, scan meta and comments for a declared
escalation: a script that states a codex-wrapper attempt already failed (e.g.
meta.description: "sol lane quota-dead; escalation per models table") is the
sanctioned escalation for the stages doing that declared work, even with no
codex-wrapper call in the script — you cannot verify the claim from the script,
take it at face value — but an unrelated review stage in the same script is still
judged on its own. When uncertain, fire=false — a false alarm teaches the agent to
ignore this nudge. Keep reasoning under 40 words and name the offending stage.

<examples>
<example fire="true">
agent(`Sweep the diff for go-correctness issues; return findings as JSON`)
An unpinned finder inherits fable; finder sweeps are the codex-wrapper agent's lane.
</example>
<example fire="true">
findings.map(f => agent(`Adversarially refute: ${f.title}`, {effort: 'max'}))
Refuters over code findings inherit fable — route them via agentType 'codex:codex-wrapper'.
</example>
<example fire="true">
agent('Write a self-contained codex prompt reviewing this diff, then run the codex skill', {model: 'sonnet', effort: 'low'})
The retired wrapper shape — a sonnet stage running the codex skill; use agentType 'codex:codex-wrapper'.
</example>
<example fire="false">
agent(`Review the diff hunks in src/ for correctness; return findings as JSON`, {agentType: 'codex:codex-wrapper'})
Routed via the codex-wrapper agent — exactly as mandated.
</example>
<example fire="false">
agent(`Synthesize the confirmed findings and decide which to fix`)
Synthesis/accept-reject stays on fable.
</example>
<example fire="false">
const r = await agent(q, { agentType: 'codex:codex-wrapper', phase: 'Review', schema: REVIEW })
if (r) return r
log('sol empty — fable fallback')
return await agent(q, { phase: 'Review', schema: REVIEW })
The unpinned Review call runs only after the codex-wrapper stage returned nothing — the sanctioned fable escalation, not misrouting.
</example>
<example fire="false">
export const meta = { name: 'p1-fable-review', description: 'Fable finder+refuter over the landed P1 commit (sol lane quota-dead; escalation per models table)', phases: [{ title: 'Review' }] }
const f = await agent(`Review the landed diff for correctness; findings as JSON`, { label: 'find:fable', phase: 'Review', schema: REVIEW })
The meta declares the codex-wrapper lane already failed — a declared escalation, not misrouting, even though no codex-wrapper call appears in the script.
</example>
<example fire="true">
const findings = await agent(`Review the diff in src/ for correctness; findings as JSON`, {model: 'fable'})
An unconditional fable review with no codex-wrapper attempt and no declared escalation — route it via agentType 'codex:codex-wrapper'.
</example>
<example fire="true">
agent(`Audit the auth flow for injection and session-fixation issues; return findings as JSON`)
An unpinned security audit inherits fable; security review/audit is the codex-wrapper agent's lane.
</example>
</examples>
