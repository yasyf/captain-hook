Decide whether the main agent should delegate this sustained browser automation
instead of driving it inline.

The main loop runs on fable-5; this session has been driving the browser directly —
a run of `agent-browser` / `playwright` calls (click, fill, snapshot, scrape, QA
step) through the recent tool calls shown below.

The Models rubric: sustained tool-driving — browser automation, QA sweeps, bulk
extract/fill/snapshot — is fable's lane, but it runs in a delegated fable subagent,
not inline on the main loop: the mechanical call run stays out of the orchestrator's
context. When the site needs the user's own login, an agent-browser-with-cookies
teammate owns it. Fable drives the browser inline only for a single gated, stateful,
or authenticated interaction it just decided to run: a go/no-go verification, one
confirming screenshot, or a login+2FA flow it must hold open.

Set fire=true only when the recent activity is a sustained mechanical run a subagent
could own end to end — several browser steps in a row with no judgment between them.
A single verification, a short authenticated flow the main loop must keep stateful, or
browser work entangled with reasoning the main agent is exercising right now stays
inline: fire=false. When uncertain, fire=false — the agent may be driving the browser
inline deliberately, and a false alarm teaches it to ignore this nudge. Keep reasoning
under 40 words.

<examples>
<example fire="true">
Twelve consecutive agent-browser calls filling and submitting sitemap URLs across
Search Console and Bing, no reasoning between them. A mechanical sweep a delegated
fable subagent should own.
</example>
<example fire="true">
A long run of agent-browser snapshot/scrape steps pulling analytics rows into a table.
Bulk extraction — a delegated fable subagent's lane.
</example>
<example fire="false">
One agent-browser screenshot to confirm the deploy the agent just shipped rendered.
A single go/no-go verification — inline is right.
</example>
<example fire="false">
An agent-browser login flow pausing for the user's 2FA code before one authenticated
action. A short stateful flow the main loop must hold open — not a delegatable sweep.
</example>
</examples>
