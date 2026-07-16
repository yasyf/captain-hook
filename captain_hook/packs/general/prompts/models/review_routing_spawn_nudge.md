Decide whether this delegated subagent runs code review, a security review/audit
or verification of security-sensitive code, or bug diagnosis that should route to
gpt-5.6-sol instead of fable.

<delegated_spawn> holds the pending Agent/Task call: its model pin (or that it inherits
the session model, fable), agent type, and prompt.

The Models rubric: code/diff review — sweeping a diff or codebase for bugs,
correctness, or cleanups; finder and refuter passes over findings — security
review/audit and verification of security-sensitive code (auth, input validation,
crypto, secrets), and bug diagnosis route to gpt-5.6-sol via the codex-wrapper agent —
spawn agent type 'codex:codex-wrapper' with the self-contained question; fable is
the escalation target when gpt-5.6-sol's output misses. Fable keeps design/architecture
review, "is this the right approach" judgment, prose review, the synthesis/
accept-reject pass over review findings — and security-sensitive implementation,
which is not review.

Set fire=true only when the prompt clearly reviews code or diffs for defects, audits
or verifies security-sensitive code, or diagnoses a bug, and the spawn would run on
fable. Design review, approach judgment, synthesis over findings, and prose review
are fable's lanes: fire=false. A spawn whose prompt states it is the escalation
after a codex-wrapper attempt that missed or returned nothing is the sanctioned
escalation: fire=false — unless that prompt runs the codex skill again; the
retired wrapper shape stays fire=true even when declared as an escalation. When
uncertain, fire=false — the agent may have
chosen fable deliberately, and a false alarm teaches it to ignore this nudge. A
model-pinned spawn whose prompt runs the codex skill itself is the retired wrapper
shape: fire=true. Keep reasoning under 40 words.

<examples>
<example fire="true">
Review the diff for correctness and concurrency issues; report findings as JSON.
Diff review for defects — gpt-5.6-sol's lane via the codex-wrapper agent.
</example>
<example fire="true">
Adversarially refute this finding: the retry loop double-increments the counter.
A refuter pass over a code finding is review work.
</example>
<example fire="true">
Diagnose why the exporter hangs when two workers flush concurrently.
Bug diagnosis starts on gpt-5.6-sol; fable is the escalation target.
</example>
<example fire="true">
You are a low-cost wrapper: write a self-contained codex prompt reviewing this diff, then run the codex skill.
The retired wrapper shape — spawn the codex:codex-wrapper agent with the question instead.
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
Security review/audit of code — gpt-5.6-sol's lane via the codex-wrapper agent.
</example>
<example fire="true">
Verify the new input-validation layer rejects path traversal and injection payloads.
Verification of security-sensitive code routes to gpt-5.6-sol.
</example>
<example fire="false">
Implement mitigations for the security-audit findings in auth.py.
Security-sensitive implementation, not review — the implementation lanes apply.
</example>
<example fire="false">
Escalation: the codex:codex-wrapper review of this diff returned no findings despite the reproduced double-close in pool.go — re-review src/pool.go and report findings as file:line JSON.
A declared escalation after a documented codex-wrapper miss — fable is the escalation target.
</example>
</examples>
