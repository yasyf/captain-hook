"""The judge's static prompt skeletons — the single source both the renderer and the versioner read.

:mod:`~captain_hook.review.judge` renders these with :meth:`str.format` per row;
:mod:`~captain_hook.review.store` hashes them to derive each lane's prompt
version, so editing a template is its own version bump. A leaf module both import
without a cycle (``judge`` already depends on ``store``).
"""

from __future__ import annotations

CREATE_TEMPLATE = """\
You are auditing one piece of feedback a developer gave an AI coding assistant
(Claude), deciding whether it is a DURABLE correction worth encoding as an
automated hook — a rule that should fire in every future session of this
repository — or feedback that only mattered in the moment.

Pick exactly one category:
- durable_style_rule: a standing code-style or API-design rule ("never use a
  bare except", "always frozen dataclasses") that future code must follow.
- workflow_rule: a standing rule about process — how to plan, commit, test,
  review, or communicate ("always run the tests before claiming done").
- tooling_rule: a standing rule about which tool or command to use ("use uv,
  not pip", "search with rg, not grep").
- safety_guard: a standing guard against a dangerous action ("never force-push
  to main", "never edit generated files").
- one_off_correction: fixes the assistant's current output without stating a
  reusable rule ("rename this one", "the test you broke is test_foo").
- task_specific: a rule scoped to the current task or file, not the repository
  ("for this migration keep both columns").
- preference_unclear: corrective in tone, but the underlying rule cannot be
  stated precisely enough to automate.
- ambient_noise: not corrective at all — status updates, questions, new tasks.

The first four categories are durable; the rest are not. A durable correction
states (or clearly implies) a rule that would be violated again and could be
checked mechanically. Words like "always", "never", or "stop doing X" are
strong durability signals; a rule that names one specific line, variable, or
test is task-scoped, not durable.

summary: ONE neutral sentence naming the rule the feedback implies (or what the
user reacted to when there is no rule). Write it for every category.
confidence: your probability (0 to 1) that your durable-vs-not call is correct.
rationale: one short clause.
rule_slug: a canonical kebab-case name for the rule, 2-6 words (e.g.
"never-bare-except", "prefer-uv-over-pip"). Reuse a suggested slug VERBATIM if
this feedback states the same underlying rule — even paraphrased, misspelled, or
captured by a different detector; mint a new slug only for a genuinely new rule;
if several fit, reuse the first listed. null for every non-durable category.

Suggested slugs (existing durable rules, most similar first):
{suggestions}

Respond with strict JSON matching the schema — no extra keys, no prose.

[source: {source_kind}]
{context}
{question_answer}=== FEEDBACK TO CLASSIFY ===
{text}"""

FIX_TEMPLATE = """\
You are auditing one remark an AI coding assistant (Claude) made about an
automated hook that fired during its session, deciding whether the remark
REPORTS A MISFIRE — the hook firing wrongly or redundantly — or something else.

Pick exactly one category:
- misfire_confirmed: the remark asserts the hook fired wrongly — it re-fired on
  content already addressed, flagged a false positive, or fired outside its
  intended scope — and the surrounding conversation is consistent with that
  claim.
- compliance: the remark acknowledges the hook's message and follows it (or
  promises to follow it going forward).
- ambient_mention: the hook is merely described, quoted, or referenced in
  passing, with no claim that it fired wrongly.

Only misfire_confirmed marks the hook as worth amending. A remark that both
complies and dismisses ("noted, but this re-fired on text I already fixed") is
misfire_confirmed — the dismissal is the signal. A remark that merely reports
the hook fired, or works around it without disputing it, is not.

summary: ONE neutral sentence naming what the hook did and what Claude claims
about it. Write it for every category.
confidence: your probability (0 to 1) that your misfire-vs-not call is correct.
rationale: one short clause.

Respond with strict JSON matching the schema — no extra keys, no prose.

[hook: {target_hook_name} ({event}/{action})]
=== the hook's fire message ===
{fire_message}
{context}
=== REMARK TO CLASSIFY ===
{text}"""
