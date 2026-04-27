# Changelog

## 0.2.0

- **`audit()` primitive** -- declarative JSONL event logging with
  customizable destination and record fields.
- **`@session_state` decorator** + `SessionStore.tracked_models()` /
  `tracked_paths()` -- register Pydantic state models for collective
  introspection. `HookState` and `PrimitiveState` are auto-tracked.
- **Default flip** for `llm_gate` / `llm_nudge`: `agent=True` and
  `transcript=True` are now defaults. *Migration:* if you were relying on
  the old non-agentic, no-transcript behavior, pass `agent=False,
  transcript=False` explicitly.
- **`BaseHookEvent.transcript_path`** -- public accessor for the current
  transcript file path.

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
