# Examples

Each page picks one capability of the framework, frames it as a problem you've actually hit, and links to a self-contained `examples/*.py` file you can drop into your hooks directory. Every example file ships with inline `tests = {...}` you can run via `captain-hook --hooks packages/captain-hook/examples test`.

| Example | Teaches |
|---|---|
| [Audit Logging](audit.md) | JSONL audit log with custom fields and hourly rotation. |
| [Code Quality](code-quality.md) | Layered detection: regex `hook` → AST `lint` → signal `nudge` → `llm_gate`. |
| [Command Safety](command-safety.md) | `block_command` plus `evt.command_line.q.*` for AST-level command predicates. |
| [Custom Condition](custom-condition.md) | The `CustomCondition` protocol for project-specific predicates. |
| [Failure Recovery](failure-recovery.md) | Signal-driven `Stop` nudge that points at debug tools when failures repeat. |
| [Multi-Step Workflow](multi-step-workflow.md) | `workflow` / `Step` / `Artifact` enforcing a checklist before `SubagentStop`. |
| [Session Workflow](session-workflow.md) | `@workflow_state` group spanning `PreToolUse`, `UserPromptSubmit`, and `Stop`. |
| [Settings Config](settings-config.md) | `HooksSettings` subclass plus a sibling hook that reads `evt.ctx.c.*`. |
| [Test Integrity](test-integrity.md) | `prompt_check` with `Prompt.from_template` detecting weakened tests. |
