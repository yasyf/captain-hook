Decide whether the main agent should delegate this inline edit instead of making
it itself.

The main loop runs on fable-5; this pending edit is fable implementing directly.
<edit_target> names the file; <before_edit>/<after_edit> hold the text being replaced
and written.

The Model Routing rubric: implementation defaults to a delegated model='opus'
subagent at every horizon. Use high for an individual bounded, decision-light
change, and xhigh for ambiguous, exploratory, decision-dense, large net-new, or
long-running implementation. Repetitive bounded N-unit sweeps never run on opus:
use sonnet at xhigh when the lanes must stay Claude-side, and gpt-6-astra at xhigh
via codex:codex-wrapper otherwise. Shell-heavy execution also routes to astra at
xhigh. Very sensitive or error-prone implementation — auth, migrations,
concurrency, data loss, crypto, or subtle algorithms — goes to a typed
model='fable' subagent, never inline. For other implementation, the inline
carve-out covers only a small change or one bound to judgment the main agent
just exercised. A missed implementation lane crosses between opus xhigh and
astra xhigh before reaching fable.

Set fire=true when the edit implements sensitive or error-prone code inline, or
when it is substantial ordinary implementation a subagent can own end to end.
Sensitivity requires a typed fable subagent even when the change is small or
bound to recent judgment. For other implementation, a small fix-up or a change
bound to judgment the main agent just exercised may stay inline: fire=false.
When uncertain, fire=false — the agent may be editing inline deliberately, and a
false alarm teaches it to ignore this nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
after_edit: a new 180-line pagination module written to src/api/pagination.py.
Delegate to opus at high when decision-light, or opus at xhigh when judgment calls remain.
</example>
<example fire="true">
after_edit: rewiring three call sites and adding a formatter class in export.py.
A routine decision-light refactor belongs on opus at high.
</example>
<example fire="false">
after_edit: a two-line fix to the retry counter the agent just diagnosed.
A small fix-up bound to judgment already exercised may stay inline.
</example>
<example fire="true">
after_edit: reworking the token-refresh lock in auth/middleware.py.
Auth plus concurrency requires a typed model='fable' subagent; it never stays inline.
</example>
</examples>

See CLAUDE.md § Model Routing (§ Plan Execution & Orchestration in repos not yet re-bootstrapped).
