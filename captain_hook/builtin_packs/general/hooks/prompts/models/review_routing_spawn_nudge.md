Decide whether this delegated subagent runs code/diff review, security
review/audit or verification of security-sensitive code, or bug diagnosis that
belongs on gpt-6.1-sol at xhigh on the standard tier through codex:codex-wrapper.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt. A spawn naming no model runs opus, whatever the session model.

The Model Routing rubric: code/diff review is sol's finder lane; add a refuter
only at audit depth. Security review/audit, verification of security-sensitive
code (auth, input validation, crypto, secrets), and bug diagnosis also route to
gpt-6.1-sol at xhigh on the standard tier. Spawn agent type 'codex:codex-wrapper'
with the self-contained question as its prompt; use Skill(codex) from the main
conversation. A Claude-model spawn that asks its agent to run the codex skill is
the retired wrapper shape. Design/architecture review, approach judgment, and
synthesis/accept-reject over findings belong on opus at xhigh. All prose/writing
runs on opus; reading a prose artifact does not make it code review. The most
sensitive implementation goes directly to a typed model='fable' subagent and is
outside this review nudge. For review or diagnosis, escalate a sol miss to opus
at xhigh. Fable is never a review or diagnosis lane.

Set fire=true when a sol-lane review or diagnosis runs on a Claude model without
a stated sol miss, when it runs on fable, or when a Claude-model spawn asks its
agent to run the codex skill itself. The retired wrapper shape stays fire=true
even when called an escalation. A codex:codex-wrapper spawn is routed correctly:
fire=false. An opus xhigh escalation after a stated sol miss is allowed:
fire=false. Design review, prose review, and implementation are outside this
nudge: fire=false; their own lanes still apply. Synthesis/accept-reject is
outside it on any route: fire=false. When uncertain, fire=false — the agent may
have chosen the route deliberately, and a false alarm teaches it to ignore this
nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
model: fable — Review the diff for correctness and concurrency issues; report findings as JSON.
Code/diff finder work belongs on gpt-6.1-sol at xhigh via codex:codex-wrapper.
</example>
<example fire="true">
model: fable — At audit depth, adversarially refute this finding: the retry loop double-increments the counter.
An audit-depth code refuter belongs on gpt-6.1-sol at xhigh; other review depths use a finder only.
</example>
<example fire="true">
Diagnose why the exporter hangs when two workers flush concurrently.
An unpinned spawn runs opus; bug diagnosis starts on gpt-6.1-sol at xhigh.
</example>
<example fire="true">
You are a low-cost wrapper: write a self-contained codex prompt reviewing this diff, then run the codex skill.
The retired wrapper shape: spawn codex:codex-wrapper with the question to reach sol at xhigh.
</example>
<example fire="false">
Judge these three sharding designs and recommend one.
Design/architecture judgment belongs on opus at xhigh and is outside this code-review nudge.
</example>
<example fire="false">
Synthesize the confirmed findings and decide which to fix before release.
Synthesis/accept-reject belongs on opus at xhigh.
</example>
<example fire="false">
agentType: codex:codex-wrapper — Synthesize the confirmed review findings and decide which to fix.
Synthesis/accept-reject is outside this nudge on any route.
</example>
<example fire="false">
Review the README draft for factual errors.
Prose review is outside this code-review nudge; any prose revisions run on opus.
</example>
<example fire="true">
Audit the session-token handling in auth/middleware.py for vulnerabilities.
Security review/audit belongs on gpt-6.1-sol at xhigh via codex:codex-wrapper.
</example>
<example fire="true">
Verify the new input-validation layer rejects path traversal and injection payloads.
Verification of security-sensitive code belongs on gpt-6.1-sol at xhigh.
</example>
<example fire="false">
model: fable — Implement mitigations for the security-audit findings in auth.py.
Implementation is outside this review nudge; the implementation lane judges the fable pin.
</example>
<example fire="false">
model: opus, effort: xhigh — Escalation: the codex:codex-wrapper review returned no findings despite the reproduced double-close in pool.go. Re-review src/pool.go and report findings as file:line JSON.
A stated sol miss permits opus at xhigh.
</example>
</examples>

See CLAUDE.md § Model Routing (§ Plan Execution & Orchestration in repos not yet re-bootstrapped).
