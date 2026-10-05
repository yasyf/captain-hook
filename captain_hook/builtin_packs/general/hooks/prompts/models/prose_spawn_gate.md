Decide whether this delegated subagent call routes a prose deliverable to a writer
other than Claude Opus.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing
verb of a prose artifact, with negated asks ("do NOT edit the docs") already
screened out. Your job is precision: is a prose artifact what this subagent is
asked to PRODUCE, and does a writer other than Claude Opus produce it?

The Model Routing rubric: all writing a user reads — READMEs, docs, changelogs,
release notes, blog posts, PR descriptions, commit messages, any user-facing text
— is written by Claude Opus. An unpinned subagent runs opus, so a model='opus' pin
or no pin routes prose correctly. Prose routed anywhere else is the misroute: a
codex:codex-wrapper spawn, a prompt that hands the writing to codex, astra, or
another gpt model, or a model pin on sonnet, haiku, or fable. A subagent that only
relays prose an Opus writer already produced, landing it verbatim, is not writing
it. Work that mentions a prose file only as a constraint ("do NOT touch the
docs"), as reading material, or as the subject of recon or review is not prose
work. Codex review or diagnosis inside an Opus subagent that writes its prose
itself is not a misroute either.

{deliverable_rubric}

Set block=true only when the prompt asks for a prose artifact and that prose is
written by codex, astra, another gpt model, sonnet, haiku, or fable. An opus or
unpinned subagent that writes the prose itself is routed correctly: block=false.
Recon, review, classification, and code work that references docs stay allowed:
block=false. When uncertain, block=false — a wrong block stops legitimate work
cold. Keep reasoning under 40 words.

<examples>
<example block="true">
model: sonnet — Write the README quickstart for this repo.
Sonnet writes the README prose; prose belongs on opus.
</example>
<example block="true">
model: haiku — Update CHANGELOG.md with an entry for the retry fix.
Haiku writes the changelog prose; prose belongs on opus.
</example>
<example block="true">
model: fable — Polish the blog post announcing the new CLI.
Fable is reserved for the most sensitive implementation, not writing; prose belongs on opus.
</example>
<example block="true">
subagent_type: codex:codex-wrapper — Rewrite the README.
The codex agent hands the README to a gpt model; prose belongs on opus.
</example>
<example block="true">
model: opus — Orchestrate the incident-retro revision. Every sentence of prose is written by gpt-6-astra at xhigh via the codex skill and landed verbatim.
The opus subagent routes the writing to astra; it should write the prose itself.
</example>
<example block="false">
model: opus — Draft the release notes for v2.
Opus writes the prose itself.
</example>
<example block="false">
model: opus — Fix the import in cli.py, have the codex skill review the diff, then draft the CHANGELOG entry yourself.
Codex only reviews; the opus subagent writes the changelog itself.
</example>
<example block="false">
model: sonnet — Fix the failing test in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it.
CHANGELOG is a constraint, not the deliverable; this is code work.
</example>
<example block="false">
model: sonnet — Review the README draft for factual errors and list them.
The subagent reports review findings; it does not write or revise the README.
</example>
<example block="false">
subagent_type: codex:codex-wrapper — Review the diff for correctness; return findings as file:line JSON.
Code review returns findings data, not prose.
</example>
</examples>
