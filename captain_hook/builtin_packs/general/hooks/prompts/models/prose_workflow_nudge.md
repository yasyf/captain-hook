Decide whether this workflow script asks a stage to author prose itself on a
Claude model without routing the writing through codex on astra.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}
The header is followed by the sentences a clause prefilter matched: each asks a
writing verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md")
already screened out. Your job is precision: does any stage have prose as its own
deliverable, and does it author that prose on a Claude model without a codex/astra
route?

The Model Routing rubric: all writing a user reads — READMEs, docs, changelogs,
release notes, blog posts, announcements, any user-facing text — routes to
gpt-6-astra at xhigh through the codex skill. A stage's deliverable is prose when
its agent() prompt asks it to write, draft, revise, or polish such an artifact.
That stage must carry agentType: 'codex:codex-wrapper', or its prompt must delegate
every sentence onward to astra through codex and land it verbatim. A model: pin
never clears it: model: takes only Claude models, fable included. An unpinned
stage runs opus. Prose keywords appearing as a constraint ("do NOT edit
CHANGELOG.md"), an ownership note, a file a stage merely reads, a meta.description,
or prose the orchestrator script assembles itself outside any agent() call are
not a stage's deliverable.

{deliverable_rubric}

Set fire=true when at least one agent() call asks a Claude model to author a
prose artifact itself without a codex/astra route. A stage carrying
agentType: 'codex:codex-wrapper' or a prompt that delegates every sentence to
astra through codex and lands it verbatim is routed correctly. If every stage
with a prose deliverable has that route, or no stage has a prose deliverable,
set fire=false. When uncertain, fire=false — a false alarm teaches the agent to
ignore this nudge. Keep reasoning under 40 words and name the offending stage.

<examples>
<example fire="true">
agent('Write the README quickstart section for the new CLI', {model: 'opus'})
The quickstart stage writes README prose itself on opus, with no codex route.
</example>
<example fire="true">
agent('Draft the docs-site page for the new CLI', {model: 'sonnet'})
The docs-page stage writes prose itself on sonnet, with no codex route.
</example>
<example fire="true">
agent('Polish the release announcement', {model: 'fable'})
The announcement stage writes prose itself; fable is the sensitive-implementation lane, not the writing lane.
</example>
<example fire="true">
agent('Fix the CLI error handling', {label: 'fix:cli', model: 'opus'}) alongside agent('Reword the troubleshooting guide and CHANGELOG bullet', {label: 'fix:docs'})
The fix:docs stage has no pin, so it runs opus and writes prose itself without a codex route.
</example>
<example fire="false">
agent('Write the README quickstart section for the new CLI', {agentType: 'codex:codex-wrapper'})
The quickstart stage routes its writing through the codex agent to astra.
</example>
<example fire="false">
agent('Orchestrate the incident-retro revision. Delegate every sentence to gpt-6-astra at xhigh via the codex skill and land it verbatim.', {model: 'opus'})
The incident-retro stage routes the writing onward; it does not author the prose.
</example>
<example fire="false">
agent('Fix the failing import in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'opus'})
CHANGELOG appears only as a constraint; the stage's deliverable is a code fix.
</example>
<example fire="false">
meta: {description: 'verify the doc claims against actual behavior'}, then agent('run the test matrix', {model: 'opus'})
The prose keywords appear in meta.description; the stage runs tests.
</example>
<example fire="false">
agent('fix the three failing tests', {model: 'opus'}), then the script itself assembles CHANGELOG.md from the results
The orchestrator assembles the prose outside agent(); the stage only fixes tests.
</example>
<example fire="false">
agent('Read docs/architecture.md and list stale sections', {model: 'sonnet'})
The stage reads and classifies docs; it does not write or revise them.
</example>
</examples>
