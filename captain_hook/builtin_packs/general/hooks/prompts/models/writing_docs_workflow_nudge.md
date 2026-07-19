Decide whether this workflow script delegates documentation or prose writing to an
agent() stage without directing that subagent to read the writing-docs skill.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}
The header is followed by the sentences a clause prefilter matched — each asks a
writing verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md")
already screened out.

Decide whether an agent() prompt delegates documentation or prose writing — a README,
docs page, CHANGELOG, tutorial, release notes, or similar deliverable — without
directing that subagent to read the writing-docs skill. Restated style rules
('technical-builder voice', 'no hype adjectives', 'first person, confident') do not
count as reading the skill; that paraphrase is exactly the failure to catch. Fire only
when a stage's deliverable is prose the writing-docs skill governs. Do not fire for code
work that incidentally mentions a doc file, for reading or reviewing docs without writing
them, or for a stage that already tells its subagent to read the skill or its references.

When uncertain, fire=false — a false alarm teaches the agent to ignore this nudge. Keep
reasoning under 40 words and name the offending stage.

<examples>
<example fire="true">
agent('Rewrite the README for the new CLI. Technical-builder voice, no hype adjectives', {model: 'opus'})
The stage's deliverable is README prose with the style rules paraphrased in place of the skill.
</example>
<example fire="true">
agent(`Draft the docs-site page for ${feature}. First-person, confident, no marketing fluff`)
A docs page delegated with restated voice rules and no pointer to the writing-docs skill.
</example>
<example fire="false">
agent('Fix the failing import in cli.py; the README already documents the flag', {model: 'opus'})
Code work that only mentions the README — no prose is produced.
</example>
<example fire="false">
agent('Rewrite the troubleshooting guide, but read the doc-writing skill and its references first')
The stage already directs its subagent to the skill — nothing to nudge.
</example>
</examples>
