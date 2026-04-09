# Examples

Real-world stories showing how captain-hook solves common problems when working with AI coding agents. Each example starts with a problem you have actually hit, then walks through the solution from simple to production-ready.

---

| Example | What you will learn |
|---|---|
| [Guard Rails](guard-rails.md) | Block dangerous commands -- `git stash`, force-push, `rm -rf` -- before they execute. |
| [Code Quality](code-quality.md) | Catch `print()` statements and bare `except:` clauses in real time with regex and AST lints. |
| [Agent Behavior](agent-behavior.md) | Detect retry loops with signal scoring and escalate from nudge to blocking gate. |
| [LLM Review](llm-review.md) | Use an LLM reviewer to catch rationalization patterns like explaining away zero values. |
| [Workflows](workflows.md) | Enforce a multi-step checklist (tests, lint, coverage) before a subagent is allowed to stop. |

## How to read these examples

Each page follows the same structure:

1. **The problem** -- a concrete scenario you have encountered.
2. **The solution** -- progressive code examples, starting simple and building up.
3. **Inline tests** -- showing exactly what gets blocked, warned, or allowed.
4. **Output** -- what the agent sees when a hook fires.

All code uses `from captain_hook import ...` and can be dropped directly into your project's hooks directory.
