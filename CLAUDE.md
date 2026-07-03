@AGENTS.md

## Claude-Specific Rules

- **Clarify via `AskUserQuestion`, never inline prose** (§ Ask Before Assuming) — concrete picks, up to 4 questions per call, batched.

## Task Tracking

Non-trivial work flows `pending` → `in_progress` → `completed`: `TaskCreate` before starting, `TaskUpdate` as you go. The task list is the source of truth — complete or explicitly defer every task before stopping.

## Plan Execution & Orchestration

Plans you author must specify, and plans you execute must enforce, that substantive work runs as **dynamic workflows** (`Workflow` tool): the script holds the loop, branching, and intermediate results; your context holds only final answers. This section is standing authorization to invoke `Workflow`. Multi-phase work runs as workflows in sequence (understand → implement → verify); read each result before dispatching the next.

Exceptions: trivial single-file edits, single file reads, and single targeted `semble`/`LSP`/`Grep` lookups stay at the main-agent level; a lone ad-hoc investigation gets one subagent (fallbacks: AGENTS.md `## Parallelize Independent Work`).

**Quality patterns**: pick per task — adversarial verify, judge panel, loop-until-dry, multi-modal sweep. Reviews and audits lean thorough; quick checks lean brief.

**Models** — route per agent, up-front by task type. Higher = better; cost = cheaper:

| Model | Cost | Int | Taste | Route here |
|---|---|---|---|---|
| fable-5 | 2 | 9 | 9 | Orchestration, review, hard planning/design/diagnosis, all prose/writing (docs, READMEs, release notes, any user-facing text — never down-route writing), and implementation that is very sensitive or error-prone. The escalation target when opus output misses the bar. |
| opus-4.8 | 4 | 8 | 8 | Default — when in doubt, opus. Implementation runs here at `xhigh` unless it belongs to fable per the row above. ~2x cheaper than fable and nearly as capable: delegate aggressively. Never "escalate" fable→opus — that's a down-route. |
| sonnet-5 | 8 | 6 | 6 | Recon and routine subagent work. Pass `model: sonnet` to `Explore` — it silently defaults to haiku. |
| haiku-4.5 | 10 | 2 | 1 | Only truly mechanical single-fact steps (classify/label one thing per item). Never judgment work. |
| gpt-5.5 | 9 | 8 | 5 | Via the codex skill: well-scoped edits to existing code (little net-new code), plateau second opinions, imagegen, rote throwaway scripts. From workflows/subagents, `model` takes only Claude models — spawn a thin `model: sonnet`, `effort: low` wrapper that writes a self-contained codex prompt and runs the codex skill. |

These are defaults, not limits: standing permission to escalate any agent whose output misses the bar — escalation means fable; judge the output, not the price tag. Intelligence > taste > cost for anything that ships. Delegating to protect the context window is not a routing cue: route by task type. `general-purpose`/`Plan` subagents inherit the session model; pass `model` whenever the table disagrees.

**Effort**: `xhigh` by default; the one exception is fable implementation, which may run `high`. `max` only after an xhigh attempt falls short. Verification runs at the same or higher model + effort tier than the work it checks.

**Phase intermediates may be broken.** In a phased plan, only the final state must be coherent. Shims, dual-mode params, and interphase adapters exist to be deleted next phase — skip them.

**Authoring requirement**: every plan must include the `## Workflow Plan` section described in AGENTS.md `## Writing Plans`. A plan without it is incomplete.

**Reusable orchestrations**: save repeatable runs to `.claude/workflows/`; they become `/` commands.
