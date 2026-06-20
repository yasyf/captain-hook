# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.11.1] - 2026-06-20

### Fixed
- The `SessionEnd` reviewer no longer prints `Hook cancelled` when a non-interactive
  command like `claude update` ends. The wired `uvx capt-hook review run` hook is now
  registered async (fire-and-forget), so Claude Code fires it without waiting on or
  cancelling it. The reviewer also skips non-interactive session ends outright — a headless
  `claude -p` run (`reason` `prompt_input_exit`) carries no corrections worth mining.

## [3.11.0] - 2026-06-20

### Changed
- GitHub fetches for `pack add` / `pack update`, and the bundled spaCy model
  download, are now resilient to rate limits. Requests authenticate via
  `GITHUB_TOKEN` and fall back to `gh auth token` for the 5000/hr authenticated
  limit. Transient failures such as 429s, 5xx responses, and dropped connections
  retry with jittered backoff, and GitHub's rate-limit headers are honored, so a
  near reset is waited out while a far-off reset fails fast with an actionable
  message. An exhausted limit now raises a clean `PackError` instead of a raw
  `HTTPError` traceback, and `pack update` reports a failed re-fetch as cleanly as
  `pack add`.

## [3.10.1] - 2026-06-19

### Fixed
- Pack and hook discovery no longer aborts when a hooks directory holds a file it
  can't import as a hook. Discovery skips test files (`test_*.py`, `conftest.py`,
  anything under `tests/`) outright, and warns-and-continues past any remaining file
  that fails to import instead of failing the whole `pack add` / discovery pass.
  `import_pack_module` now loads via `spec_from_file_location`, so a hook module that
  reads `__file__` at import time works, and a module that raises mid-import is no
  longer left half-initialized in `sys.modules`.

## [3.10.0] - 2026-06-19

### Changed
- Track spawnllm 0.3.x: requires `spawnllm>=0.3.1`, which moves `schema_for` onto
  each backend and reads results through its `Invocation` seam. `call_llm` now
  delegates to `spawnllm.call`, so Codex-backed gates and nudges read their final
  message from the result file instead of the interactive log and clean up the
  schema temp file. The `openai` and `anthropic` SDKs join the dependency tree.

## [3.9.1] - 2026-06-18

### Fixed
- The `general` pack's git-stash block hint points at jj, not git branches.

## [3.9.0] - 2026-06-18

### Added
- New builtin `go` pack: a go-test-before-commit gate (blocks `git commit` of `.go`
  paths until `go test` ran this session), `gofumpt`/`golangci-lint` toolchain guards
  (mechanical formatting/linting is owned by the commit hook + CI), and a `go mod tidy`
  nudge on module-resolution failures. Enable with `capt-hook pack add go`.

## [3.8.0] - 2026-06-18

### Added
- The session reviewer feeds and reads cc-transcript's shared correction ledger.
  Per ended session it grounds each user-correction candidate via
  `extract_correction` (source `captain-hook`) — idempotent per anchor, skipping
  the hook-misfire FIX path — and `review show` surfaces the ledger's before/after
  evidence for the PR-drafting brain.

### Changed
- Track cc-transcript v4: requires `cc-transcript>=4,<5` and `spawnllm>=0.2.0`.
  The shared decision ledger table is now `decisions` (was `decisions_v1`).

## [3.7.0] - 2026-06-18

### Changed
- `capt-hook register-hooks`, `init`, the `pack` commands, and `review enable` now
  wire captain-hook's hooks into the committed `.claude/settings.json` by default
  instead of the gitignored `.claude/settings.local.json`, so hook policy is shared
  and reviewed in version control. The non-destructive merge is symmetric: when an
  event is already wired in `.claude/settings.local.json` (a per-machine setup), it
  is deferred there instead of duplicated into the committed file, so hooks never
  double-fire.

## [3.6.0] - 2026-06-18

### Added
- Pack manifests may live at `.claude/capt-hook.toml` (preferred) in addition to
  the repo root. Discovery prefers the `.claude/` location and falls back to the
  root, so a pack repo can keep its manifest beside its other Claude Code config.
  The `hooks` path stays relative to the repo root either way, and only the
  manifest plus the `hooks` subtree are cached — the rest of `.claude/` is left
  behind.

## [3.5.0] - 2026-06-18

### Changed
- `capt-hook skills install`, `capt-hook init`, and `capt-hook review enable` now
  register the captain-hook Claude Code plugin in your committed
  `.claude/settings.json` (an `extraKnownMarketplaces` entry plus `enabledPlugins`)
  instead of copying the skills into `.claude/skills/`. Claude Code installs the
  plugin on workspace-trust, so the skills track the repository and refresh on every
  commit rather than going stale after an upgrade. Plugin skills are namespaced, so
  they are invoked as `/captain-hook:bootstrapping-hooks`.
- The session reviewer's headless brain loads the bundled plugin in place with
  `claude --plugin-dir`, so it resolves `/captain-hook:scanning-sessions` without a
  marketplace clone or install prompt.

### Removed
- The `--force` flag on `capt-hook skills install` and the file-copy install path.
  Skills are no longer vendored into `.claude/skills/`.

## [3.4.0] - 2026-06-18

### Added
- `capt-hook status` (and `capt-hook review status`): a dashboard for the session
  reviewer's corrections lifecycle. It groups every tracked correction by stage —
  watching, eligible, PR open, and the merged, closed, or stale outcomes — and for
  each one shows kind-aware progress toward its PR thresholds plus the one-sentence
  summary of what its PR would do. Open-PR state renders from cache immediately,
  then refreshes against GitHub in the background and updates in place.

## [3.3.2] - 2026-06-17

### Changed
- The `bootstrapping-hooks` skill now runs `capt-hook init` in every repo,
  including those with a committed `.claude/settings.json`. 3.3.1 made `init`
  defer to the committed file instead of double-firing, so the skill no longer
  routes committed repos through `review enable`.

## [3.3.1] - 2026-06-17

### Changed
- `capt-hook init` and `capt-hook review enable` now defer to a committed
  `.claude/settings.json`. Events (and the `review run` hook) already wired there
  are not duplicated into `.claude/settings.local.json`, so running either in a
  repo with committed capt-hook hooks no longer double-fires. `init` reports the
  deferred events.

## [3.3.0] - 2026-06-17

### Added
- Turnkey session reviewer. `capt-hook init` now enables the SessionEnd reviewer
  for the current repo — it installs the reviewer skills, wires the `review run`
  hook, and starts watching — so one command leaves the repo mining ended
  sessions into hook PRs. Opt out with `capt-hook init --no-review`, or stop a
  watched repo with `capt-hook review disable`. Outside a git repo, `init` skips
  the reviewer with a hint instead of failing.
- `capt-hook review enable` now also vendors the reviewer's skills
  (`scanning-sessions`, `authoring-hooks`) into `.claude/skills/`, so arming an
  existing repo is a single command.
- The `bootstrapping-hooks` skill is the turnkey "set up captain hook" front
  door: it scaffolds with `init` (or, in repos with a committed `settings.json`,
  runs `review enable` to avoid double-firing) up front, then surveys the repo
  and proposes guardrails.

### Changed
- The shipped judge tier default moves `small` → `medium` (Sonnet), for better
  correction mining out of the box. Override with `HOOKS_REVIEW_JUDGE_TIER`.
- `capt-hook init` output is friendlier and names what it armed, including the
  session reviewer and how to disable it.

## [3.2.0] - 2026-06-16

### Added
- Packs — named, versioned collections of hooks enabled via
  `.claude/hooks/packs.toml` instead of vendoring hook files into a repo.
  Builtin packs ship in the wheel (`general`, `python`); external packs come
  from a GitHub repo carrying a `capt-hook.toml` manifest, fetched as a
  commit-pinned tarball into `~/.cache/captain-hook/packs/`. Manage them with
  `capt-hook pack add|list|remove|update`. Local `.claude/hooks/` modules
  register first, so a local hook's decision overrides a pack hook on the same
  event.

## [3.1.0] - 2026-06-16

### Changed
- Requires cc-transcript `>=3.2,<4`.

## [3.0.0] - 2026-06-16

### Changed
- Requires cc-transcript `>=3.0,<4`.

## [2.0.0] - 2026-06-12

The platform release: captain-hook becomes the hook runtime of the cc-family
session-activity platform (cc-transcript 2.0). Breaking throughout — no
compatibility shims.

### Changed
- `evt.input` is a typed `cc_transcript.tools.ToolCall` (parsed with the
  hook-runtime degrade: a Claude Code tool-shape change yields `OtherCall`
  with a still-correct content digest instead of crashing hook fires).
  `evt.content`/`evt.old`/`evt.file` re-expressed over the platform types;
  MultiEdit exposes every span; new `evt.tool_digest`.
- `ctx.t`/`ctx.transcript`/`ctx.prior`/`ctx.turn` return
  `cc_transcript.query.Session` views. Spelling changes:
  `tool_uses.where(name=...)` → `tool_calls.named(...)`,
  `where(input_has=...)` → `where_input(...)`, `File` results → `FileRef`.
- Conditions (`UsedSkill`, `ReadFile`, `TouchedFile`, `RanCommand`,
  `InPlanMode`, `Waiting`, ...) rewritten over Session predicates; tool
  matching honors platform aliases + `mcp__` suffixes.
- Session state is keyed by the Claude session UUID; stale-state GC checks
  by-UUID transcript discovery instead of a marker file.
- Hook fires write `Decision` rows to the shared cc-family ledger
  (`~/.cc-transcript/decisions.db`, dual-written with cc-review) instead of a
  private fire log; misfire attribution joins by content digest +
  nearest-preceding timestamp (`attribute_tool`), never message substrings.
- The review pipeline mines via `cc_transcript.mining` (durable
  `ContextWindow`s with refs + labeled previews); judge prompts hydrate to
  full fidelity and degrade to a labeled summary; verdicts record fidelity
  and summary-judged rows re-judge once their windows hydrate.

### Removed
- `captain_hook/transcript/` and `captain_hook/tools.py` — the transcript
  query surface lives in `cc_transcript.query`; the typed tool calls in
  `cc_transcript.tools`. All 25 transcript-era root exports are gone.
- `fire_log.py` (replaced by the decisions ledger) and the
  `HOOKS_FIRE_LOG_ENABLED` kill switch.
- `types.py` tool-alias helpers (lifted into `cc_transcript.tools`).


## [0.11.1] - 2026-06-12

### Fixed
- `Transcript.from_path` returns an empty transcript for undecodable files
  instead of raising, and subagent discovery skips macOS AppleDouble (`._*`)
  sidecar files.

## [0.11.0] - 2026-06-11

### Added
- **The reviewer's FIX mode**: the SessionEnd scanner now also mines Claude's own
  hook-misfire complaints ("the hook re-fired on my own earlier text") from
  assistant turns — three deterministic gates (dismissal marker, compliance
  de-noise, preceding-fire fingerprint) with fingerprints enumerated from real
  captured transcripts checked into `tests/fixtures/hook_fires/`. A surviving
  complaint attributes to the exact firing hook through the fire log
  (drop-on-miss, drop-on-ambiguity, primitive-aware: a `nudge()`/`gate()` fire
  resolves to the user's `hooks/<module>.py`, never `primitives/*.py`), is
  judged under a FIX taxonomy (`misfire_confirmed`/`compliance`/
  `ambient_mention`), and — once judge-accepted (a single VERY_HIGH observation
  suffices) — the brain opens a PR that **amends** the offending hook with a
  mandatory regression test. A vanished or unattributable target is skipped,
  never PR'd. `golden_review.json` gates the marker heuristics.
- Skills: `authoring-hooks` FIX mode (amend + regression test);
  `scanning-sessions` fix branch (re-verify the target at `origin/<default>`
  HEAD; stay inside the workflow — no settings edits, no improvisation).

### Fixed
- Fire-log attribution joins by `claude_session_id` (the session UUID on
  transcript events) instead of the transcript-path hash — the same transcript
  is reachable under multiple path spellings (symlinked config dirs), which made
  path-derived joins silently miss.
- `eligible()` now requires the candidate itself to be in `watching` status, so
  `threshold-check` and the brain can never re-work a candidate that already
  has a PR or reached a terminal state.
- The headless brain's turn/budget caps were too tight to finish PR creation;
  they are now settings (`brain_max_turns=80`, `brain_max_budget_usd=5.0`), and
  the scanning-sessions skill carries an explicit run-to-completion contract
  (a text-only reply ends a headless run).

## [0.10.0] - 2026-06-11

### Added
- **SessionEnd event**: `Event.SessionEnd` and `SessionEndEvent` (with `reason`),
  wired through the testing helpers, settings template, and docs.
- **The session reviewer** (`capt-hook review`): at SessionEnd, a detached child
  mines the just-ended session for durable user corrections (the CREATE mode of
  the reviewer), judges them with a small-tier LLM verdict pass, and — once a
  correction is judge-accepted across enough distinct sessions and days — spawns
  a headless brain that drafts a new `.claude/hooks/*.py` and opens a PR.
  - `review` CLI group: `run` (the SessionEnd hook entry, guard-and-spawn only,
    always exits 0), `enable`/`disable`, `scan`, `triage`, `list`/`show`,
    `threshold-check`, `update`, `sync-prs`.
  - `ReviewStore` over cc-transcript 0.8's mining + verdicts mechanism:
    judge-aware eligibility (unjudged or judge-rejected observations never
    count), candidate grouping by content rule across sessions, PR lifecycle
    tracking (open → merged/closed/stale) via `gh`.
  - `ReviewSettings` (`HOOKS_REVIEW_*` env): session/day thresholds, judge tier
    and per-session call cap, brain turn/budget caps.
- **Skills**: `authoring-hooks` (drafting a hook from a verbatim correction, with
  the pattern catalog, API, testing references, and a new pitfalls guide) and
  `scanning-sessions` (the headless reviewer brain workflow);
  `bootstrapping-hooks` now delegates drafting to `authoring-hooks`.

### Changed
- Requires `cc-transcript>=0.8,<0.9` and `spawnllm>=0.1.3`.

## [0.9.1] - 2026-06-11

### Changed
- Full docs pass: one Diataxis mode per page, consistent `uvx capt-hook` run
  convention and product naming, next-steps links on how-to pages, and real
  regenerated output in the quickstart.

### Fixed
- `llm_gate` example hooks (`code_quality.py`, `llm_cost_control.py`) now pass
  `events=Event.PostToolUse`, so their `SourceEdits`/`Content` conditions can
  match; previously they could never fire.
- `session_workflow.py` example reads `evt.user_prompt` instead of the
  nonexistent `evt.prompt`; examples without inline tests gained them.
- Changelog: restored the missing 0.1.1 and 0.3.0–0.5.0 sections and corrected
  release dates.

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

[Unreleased]: https://github.com/yasyf/captain-hook/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/yasyf/captain-hook/releases/tag/v0.9.1
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
