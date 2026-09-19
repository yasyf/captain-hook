Decide whether the main agent should delegate this sustained browser automation
instead of driving it inline.

The main loop runs on fable-5; this session has been driving the browser directly —
a run of `agent-browser` / `playwright` calls (click, fill, snapshot, scrape, QA
step) through the recent tool calls shown below.

The Model Routing rubric: sustained tool-driving — browser automation, QA sweeps,
and bulk extract/fill/snapshot — belongs on opus at xhigh. Delegate it to a
model='opus' subagent at effort='xhigh' to drive agent-browser and return findings.
When the site needs the user's own login, use an agent-browser-with-cookies
teammate on opus at xhigh. The session's fable model does not change that route.
Keep browser work inline only for a single gated, stateful, or authenticated
interaction the main agent just decided to run: a go/no-go verification, one
confirming screenshot, or a login+2FA flow it must hold open.

Set fire=true when recent activity is a sustained browser run a subagent can own
end to end, including browser automation, QA sweeps, and bulk extraction. A
single gated verification or a short authenticated interaction the main loop
must keep stateful may stay inline: fire=false. When uncertain, fire=false — the
agent may be driving the browser inline deliberately, and a false alarm teaches
it to ignore this nudge. Keep reasoning under 40 words.

<examples>
<example fire="true">
Twelve consecutive agent-browser calls filling and submitting sitemap URLs across
Search Console and Bing, no reasoning between them. Delegate this sustained
browser run to an opus subagent at xhigh.
</example>
<example fire="true">
A long run of agent-browser snapshot/scrape steps pulling analytics rows into a table.
Bulk extraction belongs on a delegated opus subagent at xhigh.
</example>
<example fire="false">
One agent-browser screenshot to confirm the deploy the agent just shipped rendered.
A single go/no-go verification may stay inline.
</example>
<example fire="false">
An agent-browser login flow pausing for the user's 2FA code before one authenticated
action. A short stateful interaction the main loop must hold open may stay inline.
</example>
</examples>

See CLAUDE.md § Model Routing (§ Plan Execution & Orchestration in repos not yet re-bootstrapped).
