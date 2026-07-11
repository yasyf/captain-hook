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
skill is the retired wrapper shape — it is NOT routed. Fable keeps the
synthesis/accept-reject stage over findings and design/architecture judgment — and
security-sensitive implementation, which is not review.

{deliverable_rubric}

Set fire=true when at least one review or diagnosis stage would run on a Claude
model: unpinned (inherits fable), pinned 'fable', or pinned to any model with a
prompt that runs the codex skill itself. Stages routed via agentType
'codex:codex-wrapper', synthesis stages, and design judgment are routed right:
fire=false. When uncertain, fire=false — a false alarm teaches the agent to ignore
this nudge. Keep reasoning under 40 words and name the offending stage.

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
<example fire="true">
agent(`Audit the auth flow for injection and session-fixation issues; return findings as JSON`)
An unpinned security audit inherits fable; security review/audit is the codex-wrapper agent's lane.
</example>
</examples>
