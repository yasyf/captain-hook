# Changelog

## 0.1.0

Initial release.

- Declarative hook registration with `hook()`, `@on()`, and primitives
- 9 event types: PreToolUse, PostToolUse, PostToolUseFailure, Stop, SubagentStop, SubagentStart, UserPromptSubmit, Notification, PreCompact
- 12 condition types (6 current-event, 5 transcript-history, plus CustomCondition)
- Primitives: `nudge`, `gate`, `lint`, `block_command`, `warn_command`
- LLM-powered hooks: `llm_gate`, `llm_nudge`, `prompt_check`, `llm_evaluate`
- Signal scoring with regex and NLP patterns
- Multi-step workflows with artifact validation
- Typed transcript API with tool use querying
- Inline testing with `Input` / `Block` / `Warn` / `Allow`
- Session-persisted state via `SessionStore` / `SessionSlot`
- CLI: `init`, `run`, `test`, `generate-settings`
