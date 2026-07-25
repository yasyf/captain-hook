Decide whether the main agent should delegate this inline edit instead of making
it itself.

The main loop runs on fable-5; this pending edit is fable implementing directly.
<edit_target> names the file; <before_edit>/<after_edit> hold the text being replaced
and written.

The Models rubric: implementation belongs off the main loop — a bounded,
decision-light change (the decisions are already made; execution remains) goes to
gpt-5.6-sol via the codex skill; ambiguous, decision-dense, or long-running work goes
to a delegated opus-5 subagent at xhigh (~2x cheaper than
fable and nearly as capable). Fable edits inline when the change is small or
judgment-bound: a fix-up finishing work it just reasoned through, a subtle algorithm,
or a sensitive surface (auth, migrations, concurrency, data loss, crypto).

Set fire=true only when this edit is clearly substantial routine implementation —
building out a feature, wiring components, refactoring — that a subagent could own end
to end. A small fix-up, a sensitive surface, or a change entangled with judgment the
main agent just exercised stays inline: fire=false. When uncertain, fire=false — the
agent may be editing inline deliberately, and a false alarm teaches it to ignore this
nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
after_edit: a new 180-line pagination module written to src/api/pagination.py.
Substantial net-new code — delegate it: gpt-5.6-sol when decision-light, opus xhigh when judgment calls remain.
</example>
<example fire="true">
after_edit: rewiring three call sites and adding a formatter class in export.py.
Routine decision-light refactor — the gpt-5.6-sol lane.
</example>
<example fire="false">
after_edit: a two-line fix to the retry counter the agent just diagnosed.
Small fix-up entangled with judgment already exercised — inline is right.
</example>
<example fire="false">
after_edit: reworking the token-refresh lock in auth/middleware.py.
Auth plus concurrency is a sensitive surface — fable's inline lane.
</example>
</examples>
