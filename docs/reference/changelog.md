# Changelog

## 0.1.0

Initial public release, extracted from an internal monorepo where the framework was battle-tested across dozens of hooks.

Supported runtimes:

- Python 3.12+
- Claude Code as the hook host

Public APIs:

- **Registration** — `hook()`, `@on()`, plus primitives `nudge()`, `gate()`, `lint()`, `block_command()`, `warn_command()`, `audit()`, `diff_lint()`, `prompt_check()`.
- **LLM hooks** — `llm_gate()`, `llm_nudge()`, `llm_evaluate()`, `Prompt` / `Prompt.from_template()`.
- **Events** — 9 event types (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SubagentStop`, `SubagentStart`, `UserPromptSubmit`, `PreCompact`, `Notification`).
- **Conditions** — 12 condition types covering current-event (`Tool`, `FilePath`, `Command`, `Content`, `Agent`, `TestFile`, `SourceEdits`) and transcript-history (`ReadFile`, `TouchedFile`, `RanCommand`, `UsedSkill`, `InPlanMode`, `Waiting`) plus `CustomCondition`.
- **Transcript API** — `Transcript`, `Turn`, `ToolUse`, `ToolUseSequence`, `ToolUseQuery.where(name=..., raw_input=...)`.
- **CommandLine** — `cl.q.runs(...)`, `cl.q.has_subcommand(...)`, `cl.q.uses_redirect()`, `cl.q.any_command(...)`, `cl.q.contains_token(...)`.
- **State** — `@session_state`, `@workflow_state`, `SessionStore` / `SessionSlot`, `HookState`, `EchoTracker`.
- **Workflows** — `Workflow`, `Step`, `Artifact`, `text_matches`.
- **Settings** — `HooksSettings` with `planning_agents`, `waiting_tools`, `state_dir`, `log_dir`; subclassable with a custom env prefix.
- **Inline tests** — `Input`, `Block`, `Warn`, `Allow`, `InlineTests` (type alias for the test mapping).
- **CLI** — `init`, `run`, `test` (`--json` / `--verbose`), `generate-settings`.

Ships with 9 curated examples under `packages/captain-hook/examples/` and matching narrative pages under `docs/examples/`.
