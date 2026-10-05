Decide whether this workflow script routes a stage's prose deliverable to a writer
other than Claude Opus.

<workflow_script> holds the pending Workflow call's script source.
{workflow_script_header}
The header is followed by the sentences a clause prefilter matched: each asks a
writing verb of a prose artifact, with negated asks ("do NOT edit CHANGELOG.md")
already screened out. Your job is precision: does any stage have prose as its own
deliverable, and does a writer other than Claude Opus produce it?

The Model Routing rubric: all writing a user reads — READMEs, docs, changelogs,
release notes, blog posts, announcements, any user-facing text — is written by
Claude Opus. A stage's deliverable is prose when its agent() prompt asks it to
write, draft, revise, or polish such an artifact. That stage carries
model: 'opus' or no model pin, since an unpinned stage runs opus. Prose routed
anywhere else is the misroute: agentType: 'codex:codex-wrapper', a prompt that
hands the writing to codex, astra, or another gpt model, or a model: pin on
sonnet, haiku, or fable. Prose keywords appearing as a constraint ("do NOT edit
CHANGELOG.md"), an ownership note, a file a stage merely reads, a
meta.description, or prose the orchestrator script assembles itself outside any
agent() call are not a stage's deliverable. A codex:codex-wrapper stage that
reviews or diagnoses code is not a prose stage.

{deliverable_rubric}

Set fire=true when at least one agent() call routes a prose artifact to codex,
astra, another gpt model, sonnet, haiku, or fable. If every stage with a prose
deliverable runs on opus, pinned or unpinned, or no stage has a prose
deliverable, set fire=false. When uncertain, fire=false — a false alarm teaches
the agent to ignore this nudge. Keep reasoning under 40 words and name the
offending stage.

<examples>
<example fire="true">
agent('Draft the docs-site page for the new CLI', {model: 'sonnet'})
The docs-page stage writes prose on sonnet; prose belongs on opus.
</example>
<example fire="true">
agent('Polish the release announcement', {model: 'fable'})
The announcement stage writes prose on fable, which is reserved for the most sensitive implementation.
</example>
<example fire="true">
agent('Write the README quickstart section for the new CLI', {agentType: 'codex:codex-wrapper'})
The quickstart stage hands README prose to a gpt model through codex; prose belongs on opus.
</example>
<example fire="false">
agent('Write the README quickstart section for the new CLI', {model: 'opus'})
The quickstart stage writes README prose on opus.
</example>
<example fire="false">
agent('Fix the CLI error handling', {label: 'fix:cli', model: 'sonnet'}) alongside agent('Reword the troubleshooting guide and CHANGELOG bullet', {label: 'fix:docs'})
The fix:docs stage has no pin, so it runs opus; fix:cli is code work.
</example>
<example fire="false">
agent('Review the diff for correctness', {agentType: 'codex:codex-wrapper'}) then agent('Write the CHANGELOG entry for the fix', {model: 'opus'})
Codex reviews code; the changelog stage writes prose on opus.
</example>
<example fire="false">
agent('Fix the failing import in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it', {model: 'sonnet'})
CHANGELOG appears only as a constraint; the stage's deliverable is a code fix.
</example>
<example fire="false">
meta: {description: 'verify the doc claims against actual behavior'}, then agent('run the test matrix', {model: 'sonnet'})
The prose keywords appear in meta.description; the stage runs tests.
</example>
<example fire="false">
agent('Read docs/architecture.md and list stale sections', {model: 'sonnet'})
The stage reads and classifies docs; it does not write or revise them.
</example>
</examples>
