Prose means text a user will read as an artifact. Output returned to the
orchestrator or caller — structured findings (file:line, severity, scenario), a
PASS/FAIL or status report, or a verbatim relay of another tool's output — is a
data deliverable, not prose, whatever writing verbs the prompt uses; composing a
self-contained codex question writes tool input, not user-facing text. The rubric
itself mandates the codex-wrapper agent (agentType on a workflow stage,
subagent_type on an Agent/Task spawn: 'codex:codex-wrapper') for code/diff
review, security audit, and diagnosis — a stage or spawn that hands that agent
the self-contained question and relays the findings is routed exactly as
mandated (a sibling nudge enforces that shape): {verdict_attr}=false.

<deliverable_examples>
<example {verdict_attr}="false">
A codex-wrapper delegate — agentType 'codex:codex-wrapper': "Review this diff
for correctness and concurrency issues; return findings as file:line JSON."
The question is the wrapper's tool input and the relay returns findings data to
the caller — the mandated review routing, not prose work.
</example>
<example {verdict_attr}="false">
An opus-pinned delegate: "Run the smoke suite and RETURN a PASS/FAIL report
with findings and git status."
A status report returned to the caller is a data deliverable, not user-facing
prose.
</example>
</deliverable_examples>
