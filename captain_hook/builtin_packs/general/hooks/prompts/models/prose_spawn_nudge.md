Decide whether this delegated subagent call has a writer other than
gemini-4-argon produce a long-form copy deliverable.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing
verb of a prose artifact, with negated asks ("do NOT edit the docs") already
screened out. Your job is precision: is long-form copy what this subagent is
asked to PRODUCE, and does it write that copy itself instead of handing it to
gemini-4-argon?

The Model Routing rubric: long-form copy — docs, READMEs, changelogs, blog posts,
design docs, retros, release notes, plan prose, Slack, Linear, and standup copy —
is written by gemini-4-argon through `gemini -m gemini-4-argon --skip-trust -p`. The lane
hands argon the facts and the rules (voice card, slop-cop rules, length caps) and
lands the reply verbatim. Every other writer is the misroute, whatever its model:
an opus, sonnet, haiku, fable, or unpinned Claude subagent that writes the copy
itself, a codex:codex-wrapper spawn, or a prompt that hands the writing to codex,
astra, or another gpt model. Commit messages and PR titles and bodies are not
long-form copy: the lane that ships them writes them inline, on any model. A
subagent that only relays finished text, landing it verbatim, is not writing it:
a shipping or relay lane handed the title, body, or commit message (inline, in a
body file such as --pr-body-file, or quoted inside a ccx vcs ship, git commit, or
gh pr edit command it runs as given). An agent type named for shipping or
committing (ship-pr, a commit or PR-opening agent) is this lane. Words inside
that finished text, such as a PR title about docs, are content the caller wrote,
not asks to this subagent. Work that mentions a prose file only as a constraint
("do NOT touch the docs"), as reading material, or as the subject of recon or
review is not copy work.

{deliverable_rubric}

Set fire=true only when the prompt asks for a long-form copy artifact and the
subagent, or a model other than gemini-4-argon, writes it. A PR title, PR body,
or commit message is fire=false on any route. Recon, review, classification, and
code work that references docs stay allowed: fire=false. When uncertain,
fire=false — a wrong warning interrupts legitimate work. Keep reasoning under 40
words.

<examples>
<example fire="true">
model: sonnet — Write the README quickstart for this repo.
Sonnet writes the README itself; long-form copy belongs on gemini-4-argon.
</example>
<example fire="true">
model: opus — Draft the release notes for v2.
Opus writes the release notes itself; long-form copy belongs on gemini-4-argon.
</example>
<example fire="true">
model: (none) — Polish the blog post announcing the new CLI.
The unpinned Claude subagent writes the post; long-form copy belongs on gemini-4-argon.
</example>
<example fire="true">
subagent_type: codex:codex-wrapper — Rewrite the README.
The codex agent hands the README to a gpt model; long-form copy belongs on gemini-4-argon.
</example>
<example fire="true">
model: opus — Orchestrate the incident-retro revision. Every sentence of prose is written by gpt-6-astra at xhigh via the codex skill and landed verbatim.
Astra writes the retro; long-form copy belongs on gemini-4-argon.
</example>
<example fire="false">
agent_type: ship-pr, model: sonnet — Run exactly: ccx vcs ship -m "docs: ✨ write the retry guide" --pr-title "docs: ✨ write the retry guide" --pr-body-file /tmp/body.md
The caller already wrote the title and body; sonnet only runs the ship command, a relay lane.
</example>
<example fire="false">
model: sonnet — Read the diff on this branch, draft the PR title and description, then open the PR.
PR titles and bodies are written inline by the lane that ships them.
</example>
<example fire="false">
model: opus — Fix the import in cli.py, then write the commit message and ship it.
Commit messages are written inline by the shipping lane.
</example>
<example fire="false">
model: sonnet — Fix the failing test in cli.py. Do NOT edit CHANGELOG.md — a sibling owns it.
CHANGELOG is a constraint, not the deliverable; this is code work.
</example>
<example fire="false">
model: sonnet — Review the README draft for factual errors and list them.
The subagent reports review findings; it does not write or revise the README.
</example>
<example fire="false">
subagent_type: codex:codex-wrapper — Review the diff for correctness; return findings as file:line JSON.
Code review returns findings data, not prose.
</example>
</examples>
