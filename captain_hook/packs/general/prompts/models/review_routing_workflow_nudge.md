Decide whether this workflow script runs code review or bug diagnosis stages on
fable that should route to gpt-5.5.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}

The Models rubric: code/diff review stages — finder sweeps over a diff or codebase,
adversarial refuters over findings — security review/audit stages and verification
of security-sensitive code (auth, input validation, crypto, secrets), and bug
diagnosis route to gpt-5.5 via the codex skill. A stage does that correctly when it
pins model 'sonnet' at low effort and its prompt writes a self-contained codex prompt
and runs the codex skill. Fable keeps the synthesis/accept-reject stage over findings
and design/architecture judgment — and security-sensitive implementation, which is
not review.

{deliverable_rubric}

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
agent(`Synthesize the confirmed findings and decide which to fix`)
Synthesis/accept-reject stays on fable.
</example>
<example fire="true">
agent(`Audit the auth flow for injection and session-fixation issues; return findings as JSON`)
An unpinned security audit inherits fable; security review/audit is the codex-wrapper lane.
</example>
</examples>
