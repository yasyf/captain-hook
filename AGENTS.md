# captain-hook Development Guide

Declarative hook framework for Claude Code. Published to PyPI as `capt-hook`; the CLI is `capt-hook`, run as `uvx capt-hook`.

## Repository Structure

```
captain-hook/
├── captain_hook/     # The package — events, conditions, primitives, transcript, CLI
├── tests/            # Pytest suite (unit, integration, e2e install)
├── docs/             # MkDocs site (Material) — published to Read the Docs
│   └── examples/     # Self-contained example hooks (*.py) + their doc pages (*.md)
├── .github/          # CI (pytest + wheel smoke test) and PyPI release workflows
├── AGENTS.md         # This file — shared conventions
└── README.md         # Project overview
```

mkdocstrings generates the docs API reference from docstrings via `docs/gen_ref_pages.py`. Example hooks in `docs/examples/*.py` embed into doc pages via `pymdownx.snippets` and carry inline `tests = {...}` runnable with `capt-hook --hooks docs/examples test`.

## Ask Before Assuming

When the user's request has ambiguity — unclear scope, multiple plausible interpretations, undefined edge cases, or unspecified tradeoffs — stop and ask. Propose 2-4 concrete options and let the user pick, or list the assumptions you'd otherwise make and ask which ones hold. There is no such thing as too many questions; one wrong implementation costs more than ten clarifying exchanges. Default to interrogating the user when in doubt — multiple short questions early beat a wrong direction later.

## Code Review Response (Plan Re-Entry)

When the user reviews code you wrote and re-enters plan mode — whether by leaving inline diff comments, pasting a numbered list of issues, or otherwise sending review-shaped feedback after a recent edit cycle — you MUST:

0. **Delegate context-gathering to a subagent.** Spawn one `Explore` subagent with every cite (file:line + the user's verbatim comment text). Instruct it to, per cite, `Grep` the file with ~5 lines of context either side of the cited line (`-B 5 -A 5`), and only escalate to a full `Read` when the ±5-line window is insufficient (e.g. the comment refers to a function defined further up). Have it also surface sibling call sites with the same issue (Grep across the module). Use the subagent's digest as your source of truth when drafting the plan. Do NOT bulk-`Read` the cited files yourself in the main turn — it bloats the main context window before you've even started writing the plan.
1. **Draft a new plan**, not a code change. Plan-mode re-entry is the user asking "let's align on what you'll do next," not "go fix it."
2. **Inline every comment verbatim** in the plan. Each comment gets a short anchor (`#N`, the file:line if provided, or a quoted excerpt) plus the user's exact wording in a blockquote or `*"…"*` italics. Do not paraphrase. The user must be able to scan the plan and see every comment they wrote reproduced exactly.
3. **Cluster when many.** If there are more than ~5 comments, group them into themes (e.g. "T1 — Guards against impossible states") and list every verbatim trigger per theme. Address every cited line *and* extrapolate the rule to other call sites that have the same problem.
4. **Map every comment.** Maintain a "verbatim feedback table" near the end of the plan with one row per comment: `# | file:line | verbatim | cluster`. No comment may be silently dropped.
5. **Do NOT start implementing** before the plan is approved via `ExitPlanMode`. Delegating reads via #0 is fine; editing source is not.

The canonical shape is the `Overarching themes` table + per-cluster `**#N (verbatim):** *"…"*` anchors + final mapping table. When a comment is ambiguous, ask via `AskUserQuestion` rather than guessing.

### Plan follow-up questions

After you write a plan, the user may respond with questions ("why this approach?", "what about X?", "did you consider Y?") rather than approval. In that case you MUST NOT edit the plan to bake in answers. Instead:

1. **Answer the question conversationally** in your text response — explain the reasoning, the tradeoffs, and what you'd recommend.
2. **Propose options via `AskUserQuestion`** — one question per ambiguity, each with 2–4 concrete options the user can pick from. Batch related questions into one `AskUserQuestion` call.
3. **Wait for the user's choice** before editing the plan. The plan edit then reflects the user's pick, not your assumption.

Editing the plan first robs the user of the choice and forces them to diff the plan to find what you decided. Surface the decision point first.

## Parallelize Independent Work

Independent tasks dispatch concurrently. Two agents that could run at the same time must run at the same time; the orchestrator only routes, never executes. Pick the surface by who holds the plan:

- **Dynamic workflow** — default for substantive multi-step work: the script holds the loop, branching, and intermediate results.
- **Parallel subagent calls in one message** — ad-hoc independent investigations. One message, N `Agent` tool uses, results gathered in parallel.
- **Named team** — long-running peers needing agent-to-agent handoffs mid-run, via `TeamCreate`.

Single-step exception: one task, no parallel sibling, no follow-on → one subagent call is fine.

## Python Style

Target Python 3.12+. Run `uv sync --extra dev`, `uv run pytest`, and `uv build`.

**Docstrings on the public API only.** `captain_hook/types.py` and other user-facing surfaces carry Google-style docstrings; they render into the docs site via mkdocstrings. Internal helpers get none. No comments except TODOs, non-obvious workarounds, or disabled code.

@STYLEGUIDE.md

## General Rules

**Minimal changes.** Stay within scope; fix the issue, then stop.

**Match surrounding code.** Follow the conventions of the file you're in, then the module.

**No defensive coding.** No fallbacks, shims, or backwards-compat layers; no guards against impossible states. If unused, delete it. Crash on the unexpected.

**Code stewardship.** When you touch a file, fix nearby bugs, style violations, and broken tests; don't wave them off as pre-existing or out of scope. Trivial type-checker noise is the exception (see § Python Style).

**Observe, don't infer.** Inspect actual data — read fixtures, dump objects, run the code — before reasoning from assumption.

**Don't use external failures as an excuse to stop.** API quota, rate-limit, and outage errors rarely block the whole task; trace the catch sites and confirm a failure actually stops you before claiming it does.

**Mechanical linting.** CI and hooks handle formatting and import order. Leave `ruff` to them and fix only what needs human judgment. When reviewing code, don't flag mechanical lint violations (line length, whitespace, import order, trailing commas).

**Testing.** The suite lives in `tests/`; run it with `uv run pytest`. Use strict assertions and mock external dependencies while leaving the code under test real. NLP-dependent tests need the `en_core_web_sm` spaCy model and the `oewn:2025` wn lexicon provisioned, as in `.github/workflows/ci.yml`.

**Docs.** Any public API change must keep `uv run mkdocs build` green; run `uv sync --group docs` first. New example hooks need both the `.py` in `docs/examples/` and a doc page wired into `mkdocs.yml` nav.

**Git.** Commits should be atomic and scoped. One logical change per commit.

**Releases.** Tagging `v*` triggers `.github/workflows/release-pypi.yml`, which builds, publishes to PyPI via trusted publishing, and cuts a GitHub release. The version comes from the tag.
