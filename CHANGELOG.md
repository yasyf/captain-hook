# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-06-09

### Added
- `Input(tasks=[...])` inline-test support: the harness pre-populates the `evt.tasks`
  cached property, so hooks that gate on the native task store can exercise their real
  block/warn paths in inline tests (previously `evt.tasks` raised `KeyError` under the
  mock event, fail-opening to a misleading pass).

## [0.2.0] - 2026-06-06

### Changed
- Renamed the PyPI distribution and CLI command to `capt-hook` (previously published as `cc-captain-hook` with a `captain-hook` command), so `uvx capt-hook …` runs without the `--from` flag.

### Fixed
- `Waiting()` now detects background `Workflow` tool runs: a launched workflow
  counts as waiting until its completion `<task-notification>` arrives, so Stop
  gates (e.g. task/plan completion) no longer block mid-workflow.

## [0.1.0] - 2026-06-01

### Added
- Initial public release, extracted from an internal monorepo.
- Declarative hook primitives: `block_command`, `warn_command`, `nudge`, `gate`,
  `lint`, `diff_lint`, `audit`, `prompt_check`, and LLM variants (`llm_gate`, `llm_nudge`).
- Claude Code event types, condition types, transcript query API, workflows,
  session/workflow state, inline test harness, and the `captain-hook` CLI.

[0.6.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.6.0
[0.2.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.2.0
[0.1.0]: https://github.com/yasyf/captain-hook/releases/tag/v0.1.0
