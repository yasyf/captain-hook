Decide whether this delegated subagent call runs work whose deliverable is prose
on anything but fable — a non-fable pin, or no pin at all.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing
verb of a prose artifact, with negated asks ("do NOT edit the docs") already
screened out. Your job is precision: is a prose artifact what this subagent is
asked to PRODUCE?

The Models rubric: all writing a user reads — READMEs, docs, changelogs, release
notes, blog posts, PR descriptions, any user-facing text — routes to fable, and it
gets there only through an explicit model pin: a subagent naming no model runs
opus, never the session model, so a missing pin misroutes prose exactly as a wrong
pin does. Work that merely mentions a prose file — as a constraint ("do NOT touch
the docs"), as reading material, as the subject of recon or review — is not prose
work.

{deliverable_rubric}

Set block=true only when the prompt clearly asks the subagent to write, draft,
revise, or polish a prose artifact. Recon, review, classification, and code work
that references docs stay allowed: block=false. When uncertain, block=false — a
wrong block stops legitimate work cold. Keep reasoning under 40 words.

<examples>
<example block="true">
model: sonnet — Write the README quickstart for this repo.
The deliverable is README prose on a non-fable pin.
</example>
<example block="true">
model: opus — Update CHANGELOG.md with an entry for the retry fix.
The subagent itself writes the changelog prose.
</example>
<example block="true">
model: (none — an unpinned subagent runs opus; subagents never inherit fable) — Draft the release notes for v2.
Release-notes prose with no pin, so it runs opus; prose needs model='fable' spelled out.
</example>
<example block="false">
model: opus — Fix the failing test in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it.
CHANGELOG is a constraint, not the deliverable; this is code work.
</example>
<example block="false">
model: (none — an unpinned subagent runs opus; subagents never inherit fable) — Fix the retry backoff in client.py and update the failing test.
Code work with no prose deliverable; the missing pin routes it to opus, which is right for code.
</example>
<example block="false">
model: sonnet — Explore the cc-interact building blocks and report where the docs pipeline is written.
Read-only recon that mentions docs; nothing user-facing is produced.
</example>
<example block="false">
model: sonnet — Review the README draft for factual errors and list them.
Review findings go to the orchestrator; the subagent writes no user-facing prose.
</example>
</examples>
