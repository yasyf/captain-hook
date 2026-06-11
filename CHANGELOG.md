# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-06-10

### Added
- SQLite fire-log subsystem recording every hook fire.

### Changed
- Transcript parsing now wraps the `cc-transcript` core parser (`parse_event`); the
  public `Transcript`/`Turn`/`ToolUseQuery` API is unchanged. Adds a dependency on
  `cc-transcript>=0.7,<0.8`.
- Requires Python ≥3.13; LLM backends delegated to `spawnllm`.

### Removed
- The `audit()` primitive (superseded by the fire-log).

## [0.8.0] - 2026-06-08

### Changed
- The `generate-settings` command is now `register-hooks` and **writes**
  `.claude/settings.local.json` directly (atomically) instead of printing to stdout. The
  merge is non-destructive: existing non-captain-hook entries are preserved, captain-hook's
  own entries are refreshed, and entries for events you no longer subscribe to are dropped,
  so re-registering can never clobber hand-authored or third-party hooks. `init` and
  `register-hooks` now share one merge codepath.

### Removed
- The `generate-settings` command name (renamed to `register-hooks`, no alias) and its
  `--no-merge` flag. Use `register-hooks --dry-run` to print the merged JSON without writing.

## [0.7.0] - 2026-06-08

### Added
- Blocking Stop/SubagentStop gates built with `gate`, `llm_gate`, or `workflow` are now
  wait-aware by default. When no `skip_if` is given, `Waiting()` is added automatically so
  the gate skips while the agent is parked on background work and re-fires once it resumes.
  Pass any `skip_if` and the default is off, so include `Waiting()` yourself.

### Fixed
- `Waiting()` now detects a background `Workflow` or async sub-agent launched in an
  earlier turn. Previously it only inspected the current turn, so once a user or loop
  message advanced the turn boundary a still-in-flight launch went unseen and Stop
  gates fired mid-wait. The durable cases are tracked across the whole session until
  their completion `<task-notification>` arrives, while ephemeral waits such as
  `run_in_background` Bash and `ScheduleWakeup` stay turn-scoped.
- Restored the `categorize_files` and `read_json` re-exports to the package root; they
  were defined and documented but dropped from `captain_hook/__init__.py`, breaking
  top-level imports in consumer hooks.

### Changed
- The root `captain_hook/__init__.py` re-exports drop the redundant `X as X` aliasing in
  favor of plain imports (ruff `F401` is ignored for `__init__.py`).

## [0.6.0] - 2026-06-08

### Added
- `Input(tasks=[...])` inline-test support: the harness pre-populates the `evt.tasks`
  cached property, so hooks that gate on the native task store can exercise their real
  block/warn paths in inline tests (previously `evt.tasks` raised `KeyError` under the
  mock event, fail-opening to a misleading pass).

## [0.5.0] - 2026-06-08

### Added
- Namespaced public API for the file helpers; restored `diff_lint`.

## [0.4.0] - 2026-06-06

### Added
- Bundled Claude Code agent skills, installed by `init` and published as a plugin.

### Changed
- Renamed `PromptMessage` to `Prompt`.
- Dropped `__all__` everywhere; the root `captain_hook/__init__.py` re-exports define the public API.

## [0.3.0] - 2026-06-06

### Changed
- Converted the CLI from argparse to Click.
- Collapsed the styleguide DSL into a single composable `Matcher` in `captain_hook.style`.

## [0.2.0] - 2026-06-06

### Added
- `styleguide` primitive for AST style rules.
- Hooks read the native task store via `evt.tasks`, staying in sync with the real task list.

### Changed
- Renamed the PyPI distribution and CLI command to `capt-hook` (previously published as `cc-captain-hook` with a `captain-hook` command), so `uvx capt-hook …` runs without the `--from` flag.
- Relicensed from Apache-2.0 to PolyForm Noncommercial 1.0.0.

### Fixed
- `Waiting()` now detects background `Workflow` tool runs: a launched workflow
  counts as waiting until its completion `<task-notification>` arrives, so Stop
  gates (e.g. task/plan completion) no longer block mid-workflow.

## [0.1.1] - 2026-06-04

### Changed
- The CLI defaults `--hooks` and `--root` from `CLAUDE_PROJECT_DIR`; the explicit path flags are dropped.

## [0.1.0] - 2026-06-03

### Added
- Initial public release, extracted from an internal monorepo.
- Declarative hook primitives: `block_command`, `warn_command`, `nudge`, `gate`,
  `lint`, `diff_lint`, `audit`, `prompt_check`, and LLM variants (`llm_gate`, `llm_nudge`).
- Claude Code event types, condition types, transcript query API, workflows,
  session/workflow state, inline test harness, and the `captain-hook` CLI.

[Unreleased]: https://github.com/yasyf/captain-hook/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.9.0
[0.8.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.8.0
[0.7.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.7.0
[0.6.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.6.0
[0.5.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.5.0
[0.4.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.4.0
[0.3.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.3.0
[0.2.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.2.0
[0.1.1]: https://github.com/yasyf/captain-hook/releases/tag/v0.1.1
[0.1.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.1.0
