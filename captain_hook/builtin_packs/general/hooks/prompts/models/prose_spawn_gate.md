Decide whether this delegated subagent call asks a Claude model to author prose
itself without routing the writing through codex on astra.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing
verb of a prose artifact, with negated asks ("do NOT edit the docs") already
screened out. Your job is precision: is a prose artifact what this subagent is
asked to PRODUCE?

The Model Routing rubric: all writing a user reads — READMEs, docs, changelogs,
release notes, blog posts, PR descriptions, any user-facing text — routes to
gpt-6-astra at xhigh through the codex skill. A model: pin takes only Claude
models, so no pin, fable included, routes prose correctly. An unpinned subagent
runs opus; subagents never inherit fable. What clears a prose spawn is the route:
the spawn is the codex:codex-wrapper agent itself, or its prompt says the subagent
delegates every sentence to astra through codex and lands it verbatim. Work that
merely mentions a prose file — as a constraint ("do NOT touch the docs"), as
reading material, or as the subject of recon or review — is not prose work.

{deliverable_rubric}

Set block=true only when the prompt asks the subagent to write, draft,
revise, or polish a prose artifact itself on a Claude model without a codex/astra
route. A codex:codex-wrapper spawn or a prompt that delegates every sentence to
astra through codex and lands it verbatim clears the spawn: block=false. Recon,
review, classification, and code work that references docs stay allowed:
block=false. When uncertain, block=false — a wrong block stops legitimate work
cold. Keep reasoning under 40 words.

<examples>
<example block="true">
model: sonnet — Write the README quickstart for this repo.
The subagent writes README prose itself on sonnet, with no codex route.
</example>
<example block="true">
model: opus — Update CHANGELOG.md with an entry for the retry fix.
The subagent writes changelog prose itself on opus, with no codex route.
</example>
<example block="true">
model: fable — Polish the blog post announcing the new CLI.
Fable is the sensitive-implementation lane, not the writing lane; the prompt names no codex route.
</example>
<example block="true">
model: (none) — Draft the release notes for v2.
An unpinned subagent runs opus and writes the prose itself, with no codex route.
</example>
<example block="false">
model: opus — Orchestrate the incident-retro revision. Every sentence of prose is written by gpt-6-astra at xhigh via the codex skill and landed verbatim; you orchestrate and never write the prose yourself.
The spawn routes the writing onward to astra through codex; it does not author it.
</example>
<example block="false">
subagent_type: codex:codex-wrapper — Rewrite the README.
The spawn is the codex agent that reaches the astra writing lane.
</example>
<example block="false">
model: opus — Fix the failing test in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it.
CHANGELOG is a constraint, not the deliverable; this is code work.
</example>
<example block="false">
model: sonnet — Explore the cc-interact building blocks and report where the docs pipeline is written.
Recon mentions docs but asks for no prose artifact.
</example>
<example block="false">
model: sonnet — Review the README draft for factual errors and list them.
The subagent reports review findings; it does not write or revise the README.
</example>
</examples>
