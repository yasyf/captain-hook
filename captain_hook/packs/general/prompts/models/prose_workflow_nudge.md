Decide whether this workflow script pins a non-fable model on a stage whose
deliverable is prose.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}
The header is followed by the sentences a clause prefilter matched: each asks a
writing verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md")
already screened out. Your job is precision: does a PINNED stage have prose as its
own deliverable?

The Models rubric: all writing a user reads — READMEs, docs, changelogs, release
notes, blog posts, announcements, any user-facing text — routes to fable. A stage's
deliverable is prose when its agent() prompt asks it to write, draft, revise, or
polish such an artifact.

{deliverable_rubric}

Set fire=true only when at least one agent() call both pins haiku/sonnet/opus and
has prose as its deliverable. A prose stage with no pin of its own is fine, even
when other stages pin opus. A prose keyword that appears as a constraint ("do NOT
edit CHANGELOG.md"), an ownership note, a file the stage merely reads, a
meta.description, or prose the orchestrator script assembles itself outside any
pinned agent() call is not that stage's deliverable: fire=false. When uncertain,
fire=false — a false alarm teaches the agent to ignore this nudge. Keep reasoning
under 40 words and name the offending stage.

<examples>
<example fire="true">
agent('Write the README quickstart section for the new CLI', {model: 'opus'})
The stage's deliverable is README prose, pinned to opus.
</example>
<example fire="true">
stages.push(agent(`Draft the docs-site page for ${feature}`, {model: 'sonnet'}))
A docs page is user-facing prose; the pin is non-fable.
</example>
<example fire="false">
agent('Fix the failing import in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'opus'})
CHANGELOG appears only as a constraint; the deliverable is a code fix.
</example>
<example fire="false">
agent('Fix the CLI error handling', {label: 'fix:cli', model: 'opus'}) alongside
agent('Reword the troubleshooting guide and CHANGELOG bullet', {label: 'fix:docs'})
The prose stage carries no pin — it inherits fable; the opus pin is on code.
</example>
<example fire="false">
meta: {description: 'verify the doc claims against actual behavior'}, then agent('run the test matrix', {model: 'opus'})
"doc claims" lives in the description, not in any pinned stage's deliverable.
</example>
<example fire="false">
agent('fix the three failing tests', {model: 'opus'}), then the script itself assembles CHANGELOG.md from the results
The orchestrator writes the prose; the pinned stage only fixes tests.
</example>
<example fire="false">
agent('Read docs/architecture.md and list the sections that are stale', {model: 'sonnet'})
Reading and classifying docs is analysis, not a prose deliverable.
</example>
</examples>
