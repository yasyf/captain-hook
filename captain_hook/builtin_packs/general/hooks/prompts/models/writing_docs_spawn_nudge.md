Decide whether this delegated subagent call delegates documentation or prose
writing without directing the subagent to read the writing-docs skill.

<delegated_spawn> holds the pending Agent/Task call: its model pin, agent type, and
prompt, ending with the sentences a clause prefilter matched — each asks a writing verb
of a prose artifact, with negated asks ("do NOT edit the docs") already screened out.

You are watching an orchestrating agent spawn a subagent. Decide whether the pending
prompt delegates documentation or prose writing — a README, docs page, CHANGELOG,
tutorial, release notes, or similar deliverable — without directing the subagent to
read the writing-docs skill. Restated style rules ('technical-builder voice', 'no hype
adjectives', 'first person, confident') do not count as reading the skill; that
paraphrase is exactly the failure to catch. Fire only when the prompt's deliverable is
prose the writing-docs skill governs. Do not fire for code work that incidentally
mentions a doc file, for reading or reviewing docs without writing them, or for a prompt
that already tells the agent to read the skill or its references.

When uncertain, fire=false — a false alarm teaches the agent to ignore this nudge. Keep
reasoning under 40 words.

<examples>
<example fire="true">
Rewrite the README for this repo. You are fable; technical-builder voice, no hype adjectives.
README prose with the style rules paraphrased in place of the skill — the drift this catches.
</example>
<example fire="true">
Draft the release notes for v2. Keep it first-person and confident, no marketing fluff.
Release-notes prose; restated voice rules, no pointer to the writing-docs skill.
</example>
<example fire="false">
Fix the failing test in cli.py; the README already documents the new flag.
Code work that only mentions the README — no prose is produced.
</example>
<example fire="false">
Rewrite the README, but read the doc-writing skill and its references first.
Already directs the subagent to the skill — nothing to nudge.
</example>
</examples>
