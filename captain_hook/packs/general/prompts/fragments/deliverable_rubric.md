Prose means text a user will read as an artifact. Output returned to the
orchestrator or caller — structured findings (file:line, severity, scenario), a
PASS/FAIL or status report, or a verbatim relay of another tool's output — is a
data deliverable, not prose, whatever writing verbs the prompt uses; writing a
self-contained codex prompt writes tool input, not user-facing text. The rubric
itself mandates the model 'sonnet' + effort 'low' codex-wrapper shape for
code/diff review, security audit, and diagnosis — a delegate whose prompt writes
a self-contained codex prompt, runs the codex skill, and relays the findings is
routed exactly as mandated (a sibling nudge enforces that shape):
{verdict_attr}=false.

<deliverable_examples>
<example {verdict_attr}="false">
A model 'sonnet', effort 'low' delegate: "You are a low-cost wrapper: write a
self-contained codex prompt reviewing this diff, run the codex skill, and relay
its findings verbatim, prefixed 'CODEX SAYS:'."
Writing the codex prompt is tool input and the relay returns findings data to
the caller — the mandated review routing, not prose work.
</example>
<example {verdict_attr}="false">
An opus-pinned delegate: "Run the smoke suite and RETURN a PASS/FAIL report
with findings and git status."
A status report returned to the caller is a data deliverable, not user-facing
prose.
</example>
</deliverable_examples>
