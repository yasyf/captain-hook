@AGENTS.md

## Claude-Specific Rules

- **Clarify via `AskUserQuestion`, never inline prose** (§ Ask Before Assuming) — concrete picks, up to 4 questions per call, batched.

## Task Tracking

Non-trivial work flows `pending` → `in_progress` → `completed`: `TaskCreate` before starting, `TaskUpdate` as you go. The task list is the source of truth — complete or explicitly defer every task before stopping.
