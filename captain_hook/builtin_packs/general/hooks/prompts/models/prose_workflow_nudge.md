Decide whether this workflow script runs a stage whose deliverable is prose on
anything but fable — a non-fable pin, or no pin at all.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}
The header is followed by the sentences a clause prefilter matched: each asks a
writing verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md")
already screened out. Your job is precision: does any stage have prose as its own
deliverable, and would that stage run off fable?

The Models rubric: all writing a user reads — READMEs, docs, changelogs, release
notes, blog posts, announcements, any user-facing text — routes to fable, and it
gets there only through an explicit pin: a stage carrying no model pin runs opus,
never the session model, so an unpinned prose stage is misrouted exactly as an
opus-pinned one is. A stage's deliverable is prose when its agent() prompt asks it
to write, draft, revise, or polish such an artifact.

{deliverable_rubric}

Set fire=true when at least one agent() call has prose as its deliverable and does
not pin model: 'fable' — whether it pins haiku/sonnet/opus or pins nothing at all.
Only an explicit fable pin on that stage clears it. A prose keyword that appears as
a constraint ("do NOT edit CHANGELOG.md"), an ownership note, a file the stage
merely reads, a meta.description, or prose the orchestrator script assembles itself
outside any agent() call is not a stage's deliverable: fire=false. When uncertain,
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
<example fire="true">
agent('Fix the CLI error handling', {label: 'fix:cli', model: 'opus'}) alongside
agent('Reword the troubleshooting guide and CHANGELOG bullet', {label: 'fix:docs'})
The prose stage carries no pin, so it runs opus; prose needs model: 'fable' spelled out.
</example>
<example fire="false">
agent('Rewrite the quickstart section of the README', {model: 'fable'})
The prose stage pins fable — routed right.
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
