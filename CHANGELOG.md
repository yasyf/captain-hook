# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [8.15.0] - 2026-07-09

### Fixed
- **Signal scoring no longer misfires on harness-injected or scattered prose.**
  Two root-cause fixes to the signal engine, prompted by the steering
  stewardship nudge warning about "dismissing a pre-existing issue" when the
  trigger vocabulary came from skill reference docs (34 ledger fires in one
  day, one of them drawing its entire threshold from a single skill-load
  event):
  - `transcript_texts()` excludes harness-injected transcript events — skill
    loads (`isMeta`) and compact summaries — from scoring candidates. Human
    prompts, assistant text, thinking, and prose-tool inputs still count.
  - `Signals` gained a keyword-only `scope` field. The new default
    `scope="text"` requires the threshold to be met within a single candidate
    text, with `window` only bounding how far back that text may sit;
    `scope="window"` keeps the old presence-union pooling across the window
    for hooks whose tells legitimately split across messages — the steering
    deferral gate, the steering typing-warnings nudge, and the detours nudge
    are stamped accordingly. `threshold` below 1 now raises at construction.
- **Ephemeral waits match on typed calls, not raw tool names.**

### Added
- **`skip_planning_agents` opt-out on `nudge()` and `gate()`.**
- **Steering: teammate tight-digest nudge on SubagentStart.**

### Changed
- **cc-transcript 10.** Typed `TaskCall.agent_name` and alias-aware matching.

## [8.14.0] - 2026-07-08

### Changed
- **General-pack codex routing targets the codex-wrapper agent.** The
  implementation and review-routing nudges (and their judge rubrics) now steer
  delegated codex runs to the `codex:codex-wrapper` agent — `agentType:
  'codex:codex-wrapper'` on workflow stages, `subagent_type` on Agent/Task
  spawns, prompt = the self-contained question — retiring the model 'sonnet'
  stage/spawn that "runs the codex skill" (that shape now fires the nudges; the
  codex skill stays the main-conversation lane, and runs inline everywhere since
  codex plugin 0.10.0). The deliverable-rubric fragment and worked examples
  score the new shape compliant, and codex-wrapper spawns are skip-listed on
  the spawn-side nudges. General pack bumped to 0.16.0.

### Fixed
- **Review LLM-backend tests skip when no backend is authenticated.** The
  review-pipeline and review-CLI tests that call a live LLM now skip instead of
  failing when no authenticated `spawnllm` backend is present, so CI stays green
  on runners that lack one.

## [8.13.0] - 2026-07-08

### Fixed
- **`framework_frame` resolves frame paths before classifying.** `caller_dir()`
  compared raw `co_filename` spellings against `FRAMEWORK_DIR`, so a symlinked
  or `..`-carrying frame path could misclassify; it now resolves the path and
  uses `is_relative_to`, which also stops prefix collisions with sibling
  directories.

## [8.12.0] - 2026-07-08

### Added
- **`excerpt_around()` and `Excerpts` — one excerpting vocabulary for the whole
  framework.** Verbatim windowed excerpts around character spans: windows merge
  per line, `…` brackets only where text was actually elided, and a character
  budget drops overflow into an explicit `… [+N more <noun> not excerpted]`
  marker instead of losing it silently. This is the general-purpose home for the
  hand-rolled pin-header logic 8.5.1 patched into the general pack;
  `WorkflowScriptSource.pins_and_source` is now ~10 lines on top of it.
- **`WorkflowScriptSource` is a framework prompt context.** Lifted from
  `packs/general/models.py` into `captain_hook.contexts` (root-exported); the
  pack's `ProseWorkflowScript` subclasses it. The framework header says
  "inherits the session model" — naming that model (fable) moved to the pack's
  rubric fragment, where project-specific facts belong.
- **General-pack judge rubrics live in `Prompt.load()` `.md` files.** The eight
  LLM rubric prompts moved from inline string literals to
  `packs/general/prompts/models/*.md` (991 → 558 lines in `models.py`), with
  the two texts that kept drifting single-sourced as fragments:
  `deliverable_rubric.md` (the "output returned to the orchestrator is a data
  deliverable, not prose" taxonomy plus its contrastive examples, templated on
  the verdict attribute) and `workflow_script_header.md` (how to read the
  pin-excerpt header). The review-routing workflow nudge gains the taxonomy it
  never had, and the writing-docs workflow nudge sheds a header description
  that had been stale since 8.5.1. Replayed against the recovered live-misfire
  corpus: codex-relay relays 0/5 fires, smoke reports 0/3, spawn-side relays
  0/5 blocks, with pinned-prose positive controls 3/3 on both sides (9 of the
  14 negatives now die at the NLP prefilter before any LLM call).

### Changed
- **`Prompt.from_template` substitutes only `{identifier}` placeholders.** Code
  braces (`{model: 'sonnet'}`), `${...}`, and bare `{}` pass through literally,
  so `.md` prompt files can quote JavaScript without escaping. The strict
  missing-variable `KeyError` is kept; `{{x}}` is no longer an escape and
  format specs are no longer placeholders (neither had in-tree users).
  `caller_dir()` now honors the packs carve-out, so `Prompt.load` from a
  builtin pack resolves the pack's own `prompts/` directory.
- **`llm_gate` and `llm_nudge` accept a `Prompt`** as well as a string,
  matching `prompt_check`.
- **Review-store prompt versions derive from prompt content.** Each judge
  lane's `prompt_version` is a sha256-derived int of that lane's static
  template (new leaf module `review/prompts.py`); editing a prompt IS the
  bump, so "forgot to bump the constant" is unrepresentable. Stale-verdict
  purging runs once at `ReviewStore.open` — read paths self-heal too — which
  retires 8.11.0's per-pass sweep and its `purged P stale verdicts` triage
  line, and the judge queue is a plain create-then-fix concatenation, which
  retires 8.11.0's diverged-lane round-robin (a divergence that never occurred
  and self-heals on the next pass). On first deploy every existing verdict
  goes stale and the next pass re-judges the backlog: the corpus re-judge
  doubles as the data migration, as with the v2→v3 bump. `FIX_CATEGORIES` is
  gone — the stress seeds resolve a verdict's lane through
  `store.versions.for_row`, the same path `persist_verdict` uses. General pack
  bumped to 0.15.0.

### Fixed
- **The Stop reviewer reviews the diff, not the transcript.** It blocked a
  session by flagging a deliberate length tripwire in a spent one-shot workflow
  continuation script sitting in a session scratchpad — a file that was never
  in the `<diff>` and reached the judge only through the transcript window. The
  rubric now names `<diff>` as the only review subject, the transcript as
  intent context, out-of-tree files (scratchpads, temp dirs, one-shot
  continuation scripts) as never reviewable, and a deliberate guard in a
  completed one-shot as not a bug. Inline regression fixtures reproduce the
  misfire on both the review and docs-freshness gates.
- **`EditedSource` and `SourceEdits` are repo-scoped.** `EditedSource` (the
  review/docs gate condition) counted any session Edit/Write anywhere — a
  scratchpad `.js` armed the Stop reviewer for the rest of the session.
  `SourceEdits` gains `project_only=True`, closing the drift with
  `docs/guide/conditions.qmd`, which had claimed it all along; both share the
  new `is_project_path()`.
- **An empty diff skips the review.** `llm_evaluate` with `diff=True` resolves
  the diff once and skips the model call entirely (consuming no fire) when
  there is nothing to review, the same contract as a required-context miss —
  previously a clean tree left the judge staring at the transcript, exactly
  the misfire channel above. Inline hook tests get a canned non-empty diff
  from `StubbedContext`.
- **Truncation is marker-honest everywhere.** `apply_contexts` and the LLM
  context block clip with `…(+Nch)` markers instead of silent head-slices, and
  `lint`'s `format_result` appends `…(+N more)` for violations beyond
  `max_shown`.

## [8.11.0] - 2026-07-07

### Added
- **Stale judge verdicts are swept.** Each judge pass now closes with
  `ReviewStore.purge_stale_verdicts()` — a lane-aware, judge-role-scoped delete
  of verdict and slug-evidence rows recorded at a prompt version their lane no
  longer runs. Before, every version bump silently orphaned the old rows
  forever. `review triage` reports the sweep with a trailing
  `purged P stale verdicts` line, printed only when nonzero (additive to the
  parsed output contract).

### Changed
- **The CREATE and FIX judge prompts version independently.** The single
  `REVIEW_PROMPT_VERSION` constant threaded through every `ReviewStore` method
  is replaced by a frozen `PromptVersions(create, fix)` bound once at
  `ReviewStore.open`; store methods resolve their lane internally
  (`hook_complaint` rows are the FIX lane, everything else CREATE). Editing one
  prompt no longer forces a full re-judge of the other taxonomy. Both lanes
  start at the previous shared version, so existing verdicts stay live.
- When lane versions diverge, the judge queue interleaves the two lanes
  round-robin so a bumped lane's backlog never starves the other under the
  per-session call cap, and the status dashboard's `last verdict` recency is
  lane-exact — stale rows at another lane's version no longer masquerade as
  fresh judge activity.

## [8.10.0] - 2026-07-07

### Added
- **Typed Stop-payload task registry.** `BackgroundTask` and `SessionCron`, with
  `evt.background_tasks` / `evt.session_crons`, expose the arrays Claude Code
  2.1.145+ sends on `Stop`/`SubagentStop` stdin — the harness's own answer to
  "is this session parked on background work". Empty tuples on events that
  don't carry them.

### Changed
- cc-transcript pin raised to `>=9.1.0` for the `Session.notifications` API.

### Fixed
- **`Waiting()` no longer mistakes an enqueued notification for a delivered
  one.** The old check matched any `queue-operation` transcript record
  containing the tool-use-id, so a completion notification that was enqueued
  but not yet delivered counted as "arrived" — a Stop gate fired on a session
  legitimately waiting for its third background agent, 63 seconds inside that
  window. `is_waiting` is now a three-layer union: the Stop payload's task
  registry, undelivered notifications still in the transcript queue
  (`Session.notifications`), and per-launch async tracking with
  delivered-or-popped completion. Also fixed on the way: an errored `Workflow`
  launch no longer pins `Waiting()` for the rest of the session, and sub-agent
  transcripts (which carry no queue-operations at all) no longer wedge
  `SubagentStop` gates permanently open.
- **The deferral gate skips plan mode (pack 0.5.1).** It judged "stayed in
  plan mode, shipped no code" as a silent downgrade — but plan mode cannot
  ship code, and the ExitPlanMode band-aid nudge already polices plan content.
- **`workflow()` composes `Waiting()` additively with `skip_if`** instead of
  dropping it when a custom `skip_if` is passed, matching `gate`/`llm_gate`.
- **`evt.tasks` reads the current native store naming.** Claude Code writes
  task lists to `~/.claude/tasks/session-<first8>/` now; the full-session-id
  lookup read an empty list for every current session, leaving the
  `TasksIncomplete` gate inert.

## [8.9.0] - 2026-07-07

### Added
- General-pack block on jj operation time-travel (pack 0.14.0): `jj op restore`, `jj operation
  restore`, and `jj undo` are denied at `PreToolUse`, with the message steering to read-only
  inspection (`jj op show`, `jj op diff --op`, any read command via `--at-op`), file recovery
  (`jj restore --from <commit> <path>`), or a throwaway workspace
  (`jj --at-op=<op> workspace add <dir> -r <rev>`).

## [8.8.0] - 2026-07-07

### Changed
- **Signal scores aggregate across window entries (presence-union).**
  `match_signals` used to threshold each transcript entry alone, so tells split
  across two assistant messages — each scoring below threshold — never fired even
  when their sum crossed it (the constructed miss pinned in the wall_of_text f08
  suite, now flipped to fire). Scoring is per-entry-then-union: each distinct
  signal counts once toward the threshold however many entries it matches (entries
  are never concatenated, so multiline regexes cannot match across entry
  boundaries), an aggregate fire consumes every contributing entry, and
  contributors return deduplicated in window order. Per-entry fires are a strict
  subset of the new behavior for positive-weight hooks; expect a sensitivity gain
  on low-threshold multi-entry windows (steering stewardship 2/15, general detours
  2/8, show wall_of_text 3/"turn" are the predicted gainers).
- **Negative signal weights are gone; suppression is a first-class `vetoes`
  field.** `Signals` gains `vetoes: Sequence[Signal | NlpSignal]` — any veto match
  in any window entry, including already-consumed ones, suppresses the fire and
  consumes nothing. Pattern weights must now be positive and a veto's weight must
  stay at its default (`Signals` raises otherwise). The steering pack's pyright
  (−3) and deferral (−4/−2) dampers migrated to vetoes; the deferral migration is
  deliberately stronger than the old arithmetic — any "asked the user…" /
  "as requested" match now suppresses outright where enough positive tells could
  previously outscore it.
- **The consumed-signal ledger is scoped per hook.** `PrimitiveState.consumed` was
  one session-wide set, so any hook's fire spent the triggering texts for every
  other hook — and aggregation's consume-all-contributors would have let one
  hook's weak contributor silently mute an unrelated hook for the rest of the
  session. `consumed` is now keyed by hook name; `match_signals`, `llm_evaluate`,
  and `consume_signals` take the hook identity explicitly (signature change).
  Stale flat-shape `primitive_state.json` files fail validation and reset to fresh
  defaults. The turn throttle (`last_fired_at`) and the echo window remain
  session-global by design.

### Removed
- `PrimitiveState.consume_echoes` (dead code — `EchoTracker` is the live echo
  path) and the negative-weight scoring tests.

## [8.7.0] - 2026-07-07

### Added
- **`packs.toml` takes a top-level `launcher` key** — a command prefix that
  replaces the default `uvx capt-hook` in every hook command the CLI writes to
  `.claude/settings.json`, for dev trees and monorepos that run `capt-hook`
  from a checkout or subproject (e.g.
  `launcher = "uv run --project \"$CLAUDE_PROJECT_DIR\" capt-hook"`). The
  suffixes — `run <Event>`, `--async`, `review run` — stay generator-owned,
  and `register-hooks --from` overrides the launcher for that invocation.
  Launcher values render as TOML basic strings via a dedicated escaper
  (`json.dumps` would emit spec-invalid surrogate escapes for non-BMP
  characters, corrupting the file for the next read), and a non-string
  `launcher` raises `PackError` at read instead of propagating downstream.
- **Degraded pack loads are attributed and surfaced.** A hook module that
  fails to import carries its pack (`LoadError(source, exc, pack)` on
  `State.load_errors`): `capt-hook test` tags the failure line with the pack
  and adds an additive `pack` field to the JSON record, `pack list` imports
  each resolved pack and prints one line per failure, and both status
  dashboards (`capt-hook status` and `capt-hook review status`) render a red
  `HOOK LOAD FAILED` line per failure. Event dispatch is untouched — no
  runtime surfacing.

### Changed
- **`register-hooks`, `pack add`, `pack remove`, and `pack update` merge
  settings three-way.** Foreign (non-capt-hook) groups pass through untouched,
  canonical capt-hook groups are refreshed or removed as before, and any
  capt-hook command the generator would not render itself — hand-edited flags,
  a different launcher prefix — is preserved verbatim as "custom". A custom
  group owns its event: the generator writes no sibling group (nothing fires
  twice) and the group survives deferral. Breaking for API callers:
  `generate_settings` and `merge_settings` take a rendered `prefix` in place
  of `from_source`; the CLI `--from` flag is unchanged.

## [8.6.0] - 2026-07-07

### Changed
- **The session reviewer's CREATE lane groups semantically.** The judge now names
  every durable correction with a canonical kebab-case rule slug
  (`ReviewVerdict.rule_slug`, prompt v3), grounded by retrieval: each judged
  event's evidence embeds into the review store, and the prompt carries the
  nearest existing slugs with their linked sentences so paraphrases, typos, and
  cross-detector duplicates of one rule reuse one slug instead of freezing as
  separate candidates. After every judge pass, `regroup_create` re-parents
  observations onto slug-keyed candidates (the `rule` field is a content digest
  before judging and the canonical slug after), retires watching candidates whose
  evidence the judge fully rejected (new `watching → rejected` edge — `rejected`
  no longer implies a closed PR), and sweeps emptied husks. `review triage`
  reports the new counts (`judged N, failed N, pending N, merged M, retired R`)
  plus a `possible split:` line per near-duplicate slug pair, and `capt-hook
  status` gains a judge segment (pending backlog, last-verdict age, slug splits).
- A user message ingested by both `transcript_message` and a more specific
  detector (`plan_review`, `interrupt_rejection`, `review_comment`) under one
  `event_uuid` now collapses to the specific detector's signal at scan time,
  halving judge spend on doubled messages. Ingest writes each candidate and its
  observation in one transaction, so a concurrent reviewer's regroup sweep can
  no longer strand a half-written pair.
- Depends on `cc-transcript[judge]>=9.0.1`: model-free verdict identity (a judge
  backend flip no longer re-judges the stored corpus), native `canonical_key`
  storage, and the sqlite-vec retrieval layer.

## [8.5.1] - 2026-07-05

### Fixed
- **Model-pin headers quote the pin, not the line head.** `WorkflowScriptSource`'s
  header truncated each pinning line to its first 200 characters, so a long
  single-line `agent()` call with opts at the end showed neither its `label:` nor
  its `model:` — the routing classifier read the stage as unpinned and nudged
  already-correct workflows. Excerpts are now verbatim windows around every
  `model:` match, `…`-marked only where text is elided, and the header is budgeted
  (`PIN_EXCERPT_CAP`) with an explicit `+N more model pins not excerpted` marker so
  a minified many-pin line cannot crowd the script out of the context cap.
- **Findings relays are no longer "prose."** The workflow prose nudge and the spawn
  prose gate read codex-wrapper review relays and PASS/FAIL status reports as prose
  deliverables and nudged them toward fable, contradicting the sibling nudge that
  mandates the sonnet/low codex-wrapper shape for review stages. Both rubrics now
  state that output returned to the orchestrator — structured findings, status
  reports, verbatim relays — is a data deliverable, with contrastive examples.
  Replayed against the captured misfires: codex-relay 4/5 → 0/5, smoke-report
  3/3 → 0/3; a genuinely pinned prose stage still fires 3/3.

## [8.5.0] - 2026-07-05

### Changed
- **`UserPromptSubmit` signal hooks now scan recent conversation, not just the
  prompt.** `transcript_texts` used to short-circuit `UserPromptSubmit` to the
  user's prompt alone, so a signal nudge could never fire on "assistant dumps
  options as prose, user replies 'hmm'" — the wall lived in the prior assistant
  turn, which the prefilter never saw, and coverage rode entirely on
  `PostToolUse`. The prompt is now prepended as its own entry to the same
  `Signals.window` scan every other event gets; `window=0` keeps a hook
  prompt-only, and `window="turn"` scans the whole prior turn (a fixed window
  counts raw events, so tool traffic can crowd the target text out of a small
  one). Per-entry scoring is unchanged.
- The builtin multi-request nudge judges the user's prompt alone (`window=0`).
  Its old `window=1` was inert under the short-circuit, and would otherwise have
  read the prior assistant turn's numbered lists as "several distinct requests".

### Fixed
- **Review-backend LLM verdicts run in untrusted and non-git directories
  again.** spawnllm's codex backend refused such cwds ("Not inside a trusted
  directory"), and `llm_evaluate`'s fail-safe swallowed the error — every
  review-backend `llm_nudge`/`llm_gate` silently no-opped there. The dependency
  floor moves to spawnllm 0.5.5, which passes `--skip-git-repo-check`; the
  read-only sandbox already confines the run. Regression tests pin the
  wall-of-text gate math on the motivating rubric shape at both `PostToolUse`
  and `UserPromptSubmit`.

## [8.4.1] - 2026-07-05

### Changed
- Dependency floors moved to the latest cc-family releases
  (`cc-transcript>=8.1`).

## [8.4.0] - 2026-07-05

### Added
- The session reviewer ingests cc-transcript 8.1's `ask_user_question` detector instead
  of crashing on it: answered `AskUserQuestion` rounds mine as `question_answer`
  correction candidates, keyed by question + answer text so multi-question rounds never
  collide and identical freeform answers coalesce across sessions. The judge prompt
  renders the question and the resolved pick above the answer under judgment. Previously
  any session containing an answered question round crashed the scan with
  `AssertionError: ask_user_question`.

### Changed
- Signal scoring reads all transcript prose, not just visible text: `transcript_texts`
  yields each thinking block and the prose fields of prose-carrying tool calls
  (`ReportFindings` findings, `TaskCreate`/`TaskUpdate` subjects and descriptions,
  `TodoWrite` todos) as separately scored entries, so a tell buried in extended thinking
  or a tool payload trips a signal hook. `Signals.window` accepts a `"turn"` sentinel
  that scores the whole current turn instead of a fixed slice of recent events — tool
  calls eat fixed-window slots, so `window=10` at `Stop` reaches only the last few tool
  exchanges.
- The steering deferral gate fires mid-turn: `PostToolUse`/`Stop`/`SubagentStop` with
  `window="turn"`. The motivating miss carried its deferral only in a thinking block and
  a `ReportFindings` payload, hundreds of events before the turn ended, with no
  turn-final text footprint — the gate now catches both at the moment of utterance. The
  judge distinguishes deliberation from commitment (naming a band-aid in order to reject
  it is not deferral, and deferral debt a review finding reports is not the reviewer's
  own), and new signals close the terse task-punt evasions ("Punt the real fix", "Defer
  the rewrite to a later pass", bare "workaround"). Steering pack bumped to 0.5.0.

## [8.3.0] - 2026-07-03

### Added
- Claude plugins can ship a capt-hook pack by session attachment. A plugin wires a
  SessionStart hook to `uvx capt-hook pack attach <dir>` (which reads the SessionStart
  JSON on stdin, validates the pack manifest, and records it under the session, printing
  nothing) and wires every per-event command to the byte-identical canonical
  `uvx capt-hook run <Event>`. Claude Code's exact-command-string dedup then collapses the
  plugin's wiring and the project's own into one process per event, instead of spawning a
  second `uvx` that re-resolves and double-dispatches the project's `packs.toml` packs.
  Attached packs resolve after `packs.toml` — a builtin or `packs.toml` pack of the same
  name wins over an attach — and a new `[packs.<name>] disabled = true` entry declines a
  pack from any source (builtin, `packs.toml`, or attach). Events subscribed only by
  attached packs are excluded from the settings-drift warning, since a plugin wires them in
  its own `hooks.json` rather than the project's settings. New guide: "Ship a pack in a
  Claude plugin".

## [8.2.0] - 2026-07-03

### Changed

- The general pack's review/diagnosis→gpt-5.5 nudges (pack 0.13.0) now also route
  security review/audit and verification of security-sensitive code (auth, input
  validation, crypto, secrets) to gpt-5.5 via the codex skill. Both prefilters
  match security/vulnerability language, and verification prompts only when a
  security noun sits nearby — plain verification spawns never invoke the judge
  (inline-tested). Security-sensitive implementation stays on fable per the
  Models table, whose Effort line now counts table-routed gpt-5.5 lanes as
  same-tier.

## [8.1.0] - 2026-07-03

### Added
- General-pack delegated-prose nudges (pack 0.12.0): a `PreToolUse` pair over Agent/Task
  prompts and Workflow scripts that delegate doc writing — a README, docs page, CHANGELOG,
  release notes, tutorial — without directing the subagent to read the writing-docs skill.
  Both reuse the `ProseSpawn`/`ProseWorkflowScript` clause prefilter (so an LLM only judges
  prompts whose deliverable is prose) and stand down when the prompt already references the
  skill. Restated style rules ("technical-builder voice", "no hype adjectives") are exactly
  the drift they catch — a paraphrase silently overrides the skill.
- Steering-pack deferral gate: an LLM-judged `Stop`/`SubagentStop` gate that blocks a
  turn-end where the agent names the correct fix, declares it blocked on a release, a
  version bump, or an upstream or cross-repo change, and silently substitutes docs, help
  text, README copy, or a follow-up issue the user never asked for. A weighted signal
  pre-filter keeps the judge off honest stops, and asking the user how to proceed
  suppresses it, since asking is the sanctioned escape hatch. The band-aid plan judge
  gained the same two tells: the fix-for-docs swap and "requires a release" framed as a
  blocker. Steering pack bumped to 0.4.0.

## [8.0.0] - 2026-07-03

### Added
- General-pack `comments` nudge (pack 0.11.0): a language-agnostic `PreToolUse` warn that
  discourages verbose comments. A `LongCommentIntroduced` custom condition diffs the edit's
  pre-image against its new text via `introduced_comments`, groups adjacent line-comment runs
  (a block comment, or consecutive `//`/`#` lines with no code between), and fires when an
  introduced run exceeds ~4 lines or ~300 characters. Diff-based, so a pre-existing long
  comment re-saved unchanged never fires; purely numeric, so no LLM call. The strict threshold
  warns on long doc runs (godoc/rustdoc) too, by design — verbosity of any kind gets a nudge;
  Python docstrings are string nodes, not comments, so they never trip it.

### Changed (BREAKING)
- **cc-transcript v8: the command-parsing core moved upstream.** `captain_hook/command.py`
  is deleted; `Command` (né `ParsedCommand`), `CommandLine`, `CommandLineQuery`, and
  `Redirect` now live in `cc_transcript.command` and are re-exported at the root. The
  top-level `Command` export is therefore the parsed-shell dataclass, no longer the regex
  condition — the condition stays at `captain_hook.types.Command` (packs import it as
  `Command as CommandCondition`). `CommandLine.primary`/`head` return `Command | None`, and
  `evt.command_line` rides upstream's lru-cached `parse_command_line`.
- **Structural matching goes through the `ast_grep` seam.** `CommandLine.matches`/
  `rewrite`/`capture` went with the moved module; call the `captain_hook.ast_grep` free
  functions instead: `ast_grep.matches(cl.raw, "bash", pattern)`, `ast_grep.rewrite(cl.raw,
  "bash", pattern, replace)`, `ast_grep.capture(cl.raw, "bash", pattern)`. `rewrite_command`'s
  structural form is unchanged in behavior.
- **`RanCommand` takes variadic argv tokens instead of a regex.**
  `RanCommand("uv", "run", "pytest", subagents=True)` matches via upstream
  `Session.has_command`/`Command.runs`: wrapper-transparent (`sudo`/`env`/`timeout` stripped)
  and launcher-literal (`uv run pytest` ≠ `pytest` — list each spelling as its own `skip_if`
  entry). Packs enumerate launcher variants explicitly; regex matching of the *current*
  event's command stays on `captain_hook.types.Command`/`Command.matches`.
- Dependencies: `cc-transcript>=8,<9`; the direct `tree-sitter`/`tree-sitter-bash` pins are
  dropped (they arrive transitively via cc-transcript). `ast-grep-py` stays.
## [6.5.0] - 2026-07-03

### Added
- General-pack `detours` nudge (pack 0.10.0): an LLM-judged `PostToolUse` warn that catches
  the agent veering onto side work nobody asked for — gated cheaply on detour phrasing
  ("while I'm here", "might as well", "let me also") co-occurring with an action-shaped tool
  call, then judged against the user's actual request in the transcript. Prerequisites,
  pre-authorized asides, and small stewardship fixes stay silent, as does anything uncertain.
  The warn tells the agent to stop, surface what it noticed, and offer 2-4 concrete options —
  or, for a delegated agent, to return early with findings plus options for its orchestrator.

## [6.4.0] - 2026-07-03

### Added
- General-pack main-loop implementation nudge (pack 0.9.0): a substantial routine
  Edit/Write on the main loop — fable implementing directly instead of delegating —
  gets an LLM-judged warn pointing at an opus xhigh subagent or the codex edit lane.
  Deterministic gates keep the judge cheap and quiet: code-file globs only, test
  files skipped, 400-char minimum, main loop only, one fire per session. A new
  `InlineEdit` context names the target file; the ambient before/after-edit blocks
  carry the text.
- General-pack review/diagnosis routing nudges (pack 0.9.0): an Agent/Task spawn or a
  workflow script that runs code/diff review or bug diagnosis on fable gets an
  LLM-judged warn routing it to gpt-5.5 via the codex skill (sonnet low-effort
  wrapper), with fable as the escalation target. Design/architecture review, prose
  review, and the synthesis/accept-reject pass stay silent — those are fable's lanes.

### Changed
- The fable-implementation delegation nudge no longer lists review and diagnosis among
  fable's lanes; code/diff review and bug diagnosis belong to the new gpt-5.5 nudges.
- `WorkflowScriptSource` is now the generic script context (pins header + truncated
  source); the prose prefilter moved to the `ProseWorkflowScript` subclass used by the
  prose-routing workflow nudge.

## [6.3.0] - 2026-07-03

### Changed
- General-pack prose-routing prefilters upgraded from proximity regex to dependency-parsed
  clause matching (pack 0.8.0): the judge is consulted only when a sentence asks a writing
  verb (matched by lemma, so "updating" and "rewriting" now count) of a prose artifact as
  its grammatical object, and negated asks ("do NOT edit CHANGELOG.md") are subtracted
  outright — that false positive no longer needs the judge at all. Scanned text is de-noised
  for the tagger first: path/URL tokens dropped, brackets and word-edge quotes blanked,
  readme/changelog extensions stripped, intra-word hyphens split, and imperative writing
  verbs given a determiner ("Update CHANGELOG.md" otherwise parses as a noun compound).
  Matched sentences are quoted to the judge as evidence in both contexts; the gate wraps
  `DelegatedSpawn` in a new prose-gated `ProseSpawn` context.

## [6.2.0] - 2026-07-03

### Changed
- General-pack prose-routing hooks are judge-confirmed (pack 0.7.0). Both the Agent/Task
  prose block and the workflow-script prose nudge keep their regex conditions as a recall
  prefilter, but a small-model judge now decides whether the prose mention is the pinned
  stage's *deliverable* — a constraint ("do NOT edit CHANGELOG.md"), an ownership note, a
  meta.description, recon over docs, or prose the orchestrator writes itself no longer
  fires. The block became an `llm_gate` (still blocking, now fail-open on LLM error), and
  `Explore`/`claude-code-guide` recon skips it entirely — it previously fought the
  Explore→sonnet auto-pin. The prefilters themselves tightened from keyword-anywhere to a
  writing verb within four words of a prose noun, so bare `docs/` paths, audit stages, and
  routing-rationale comments never reach the judge. Workflow scripts reach the judge via a
  new `WorkflowScriptSource` context (inline `script` or `scriptPath`, model-pin header,
  14KB cap).

### Added
- `Input(llm={...})` per-test LLM stub overrides: inline tests can wire-test an LLM hook's
  judge-declines path (`llm={"fire": False}` / `llm={"block": False}`); the default stub
  still always fires. `workflow_script_source` and `workflow_opt_values` are exported for
  pack-authored contexts over workflow scripts.

## [6.1.0] - 2026-07-03

### Added
- `Event.PermissionRequest` + `PermissionRequestEvent`: fires when a permission dialog would
  be shown; a hook's allow/block/rewrite answers the dialog (block maps to a deny with the
  message shown to the user), while `None` and warn fall through so the dialog shows. The
  event carries the full tool payload plus `permission_suggestions` and the asking teammate's
  `agent_type`; the `rewrite*` helpers moved to a new `ToolRewriteEvent` base shared with
  `PreToolUseEvent`, and every event gained a `skip_permissions` accessor (a process-tree walk
  for `--dangerously-skip-permissions` at launch).
- Permission primitives `approve`, `deny`, and `llm_approve` (+ `SafetyVerdict`): answer
  matching `PermissionRequest` dialogs with allow or deny, with no fire cap. `llm_approve` is
  an LLM safety judge replicating Claude Code's non-invocable auto-mode classifier, with a
  rubric seeded from `claude auto-mode defaults`, cached globally and keyed by
  `claude --version`, plus a static fallback; an unsafe verdict or LLM failure falls through
  to the real dialog, never an auto-deny.
- Conditions `FromSubagent()`, matching when the event payload carries an `agent_id` (a
  subagent/teammate origin, distinct from `Agent`'s type match), and `SkipPermissions()`,
  matching when the nearest `claude` ancestor process was launched with
  `--dangerously-skip-permissions` or `--allow-dangerously-skip-permissions`; bypass
  availability counts as consent.
- Testing surface for permission hooks: `Input` gained `agent_id` and `skip_permissions`
  seams, `Allow(explicit=True)` rejects a `None` result, and the new `Ask()` expectation
  asserts the hook returned no result (the dialog shows).
- Builtin pack `fixes` (0.1.0) for upstream Claude Code workarounds, seeded with a
  `teammate_permissions` hook for anthropics/claude-code#73176: teammates don't inherit the
  leader's `--dangerously-skip-permissions` consent, so their Bash calls pop dialogs in the
  lead UI. The hook auto-approves teammate Bash only when the process tree shows the flag,
  with a denylist covering `rm`, `sudo`, `git reset`, force pushes, and pipe-to-shell that
  falls back to the dialog.
- General-pack docs-freshness Stop gate (pack 0.6.0): after source edits, an `llm_gate` reads
  the uncommitted diff before the agent stops and blocks once when a user-facing change — a new
  flag, a renamed command, changed output, a new feature — isn't reflected in README.md or
  `docs/`. Stands down when the session already touched markdown, used the writing-docs skill,
  or runs headless (new `Headless` condition on `CLAUDE_CODE_ENTRYPOINT`). `EditedSource` and
  `NON_SOURCE_SUFFIXES` moved to a shared `packs/general/_lib.py`; review.py re-exports them.

### Fixed
- Inline-test tool resolution under a multi-tool `Tool` condition: `run_inline_tests` built
  every implicit `Input` as the first named tool. A single-name `Tool` condition still pins
  that tool; under a multi-name condition the tool inferred from the Input's field shape
  (`old` + `content` → Edit, `content` alone → Write) wins when it is among the named tools,
  and the first named tool pins otherwise. With no `Tool` condition, inference alone decides.

## [6.0.0] - 2026-07-03

### Added
- General-pack `models` fable-implementation nudge (pack 0.5.0): an `Agent`/`Task` spawn that
  would run on fable (unpinned or `model='fable'`) with a routine-implementation prompt gets an
  LLM-judged warn pointing at opus `xhigh` (or the codex skill behind a sonnet wrapper for
  well-scoped edits). Judged rather than pattern-matched — review, writing, hard planning, and
  sensitive implementation are fable's lanes and stay silent, as does anything uncertain. The
  spawn's model pin, agent type, and prompt reach the judge via a new required `DelegatedSpawn`
  context; explicit opus/sonnet/haiku pins and `Explore`/`claude-code-guide` recon skip entirely.
- `Event.SessionStart` + `SessionStartEvent`: fires on session startup, resume, clear, and
  compact (`evt.source`); it cannot block — warnings surface as `additionalContext`. Inline
  tests drive it via `Input(source=...)`.
- Eager NLP provisioning: pack manifests declare `nlp = true` (general and steering both do;
  general bumped to 0.4.0, steering to 0.3.0). `init` / `register-hooks` / `pack add|remove|update` download the
  spaCy model and oewn lexicon right after the settings write, echoing what they fetch, and an
  async `SessionStart` hook (`ensure_nlp_resources()`) self-heals installs that were offline.
  The oewn download is now filelock-guarded (it previously raced concurrent sessions). Non-NLP
  projects get neither the `SessionStart` settings entry nor the download.
- `contexts=` on `llm_gate`/`llm_nudge`: declarative `PromptContext` providers attach named
  XML evidence blocks at evaluation time; a `required` context with no evidence skips the LLM
  call entirely, consuming no fire. Built-ins: `BeforeEdit`/`AfterEdit` — ambient defaults
  carrying the pending edit's before/after text via the new `ToolHookEvent.replaced` pre-image
  (Edit's `old`, MultiEdit's joined olds, Write's on-disk content) — and
  `Introduced(kind=… | pattern=…)`, which diffs AST constructs the edit newly introduces,
  filtered through `keep()`; tags auto-derive from the class name. User-passed contexts
  with no `signals`/`when` suppress the transcript `<context>` fallback; signals compose
  unchanged. `PromptContext`, `apply_contexts`, and the built-ins are all exported.
- ast-grep comment primitives: `parse()` returns a `SyntaxNode` wrapper
  (`kind`/`text`/`descendants()`/`to_match()`) so the binding node stays behind the seam;
  `find_kinds` walks the tree instead of the kind matcher (which raises on kind names a
  grammar lacks); `comments()`/`introduced_comments()` extract comments language-agnostically
  via the exported `COMMENT_TYPES` kind union.
- Verb-anchored, morphology-aware `Clause`: `noun` is optional and the anchor derives from it
  or `verb` (VERB/AUX lemma hits, so `Phrase("be")` matches copular "was"); a new `completed=`
  gate and `subject="no_nominal"` veto, backed by the public `is_past_predicate` /
  `has_nominal_subject` predicates. `Clause` is now kw_only; keyword call sites (all known
  consumers) are unaffected.
- General-pack `tombstones` hook: a comment that narrates the edit itself (`# removed the
  retry logic`, `// no longer needed`) is caught at PreToolUse — an introduced-comments AST
  gate feeds an NLP tombstone matcher whose survivors reach a small-model judge; the warn says
  to delete the comment, not restore the removed code.

## [5.1.1] - 2026-07-02

### Fixed
- Tightened the `cc-transcript` pin to `>=7.1,<8`: 5.0.0 started importing `WorkflowCall`,
  which only exists in cc-transcript 7.1+, so a resolver landing on 7.0.x failed with an
  `ImportError` at `captain_hook` import time.

## [5.1.0] - 2026-07-02

### Added
- General-pack prose-routing hook: an `Agent`/`Task` call pinned to a non-fable model whose
  prompt is prose/writing work (a writing verb plus a prose artifact — README, docs, blog,
  changelog, release notes, …) is blocked and routed to fable; mechanical operations on text
  (classify/label/count/extract) stay exempt. A matching workflow-script nudge warns when a
  script pins non-fable models alongside prose stages. General pack bumped to 0.3.0.

### Fixed
- The haiku subagent block over-blocked legitimate mechanical work: the escape now also
  matches probe/ping/echo/smoke/count/capacity/extract stems, accepts an explicit
  `mechanical` assertion in the prompt, and the block message teaches that retry path
  instead of only steering to sonnet.

## [5.0.0] - 2026-07-02

A DX-audit release: the condition and primitive vocabularies were reshaped so the obvious
spelling is the correct one. Majors are free here — the fleet consumes `capt-hook@latest`
via uvx — so back-compat shims were not kept where a clean signature reads better.

### Changed (BREAKING)
- `captain_hook.Command` is now the **condition** (`Command(pattern)`); the parsed-shell
  dataclass it previously named is now `ParsedCommand` (still reachable as
  `captain_hook.command.ParsedCommand`). Every `from captain_hook import Command` guard
  now resolves to the condition the docs always showed.
- `ReadFile(*globs)` matches with `fnmatch` globs (like its twin `TouchedFile`) instead of
  substring containment — `ReadFile("*.md")` now works; anchor a directory match with a
  leading `**/`.
- `register()` is removed. Use `hook()` for declarative hooks and `@on()` for handlers.
- `hook(events, message, ...)` takes `message` as a required positional-or-keyword argument
  (it was a keyword that raised at runtime when omitted). Keyword call sites are unaffected.
- `Step(check=..., message=...)`: `stopped_at`/`next_step` collapse into one `message`, and
  `name` is optional (labeling only).
- A blocking `gate()`/`llm_gate()` now defaults to **unlimited** fires (it must keep
  enforcing across turns); `max_fires=None` means unlimited everywhere.

### Added
- `WorkflowScript(pattern=None, **opts)` — any `agent()` opts key is matchable
  (`WorkflowScript(model="haiku")`, `WorkflowScript(effort=r"^low$")`, …); `pattern` and
  multiple opts AND together.
- `Runs(*argv)` — structural Bash condition matching an argv prefix (`Runs("git", "stash")`),
  no whitespace-regex and no `echo git stash` false positives.
- `And(*conds)` / `Not(cond)` combinators, and `Or` is now exported.
- Variadic `Tool(*names)`, `Agent(*names)`, `UsedSkill(*names)` (pipe strings still accepted);
  `UsedSkill` matches plugin-qualified skills (`UsedSkill("codex")` matches `codex:codex`).
- `ToolInput(**fields)` — kwargs form ANDing multiple fields, with scalar coercion so
  `ToolInput(run_in_background="true")` matches a JSON `true`.
- `Input` gains `script=` (synthesizes a Workflow call), `output=`/`error=` (tool results for
  PostToolUse/PostToolUseFailure), and a working `prompt=` for Agent/Task; its tool is inferred
  from the fields set instead of silently defaulting to Bash, and its `repr` shows only set
  fields. Testing `Rewrite(**fields)` substring-matches `updated_input`.
- `block_command`/`warn_command` gain `only_if=`/`skip_if=`; `rewrite_code`/`rewrite_command`
  gain `skip_if=`. `when=` composes with `signals=` as a veto. `gate()` has a full explicit
  signature. `gate`/`llm_gate` `skip_if` is additive with the automatic `Waiting()` guard.
- Registration validates event compatibility: a tool-input condition on a non-tool event
  (e.g. `Command(...)` on `Event.Stop`) raises instead of silently never matching. Regex
  conditions compile at construction rather than at dispatch.

### Fixed
- `capt-hook test` fails (non-zero) when a hook file errors on import, reporting the
  traceback, instead of swallowing it as a warning and passing green.
- Inline-test `Block(pattern=)`/`Warn(pattern=)` with no produced message now fails the
  assertion instead of passing vacuously.
- `Command` matches the raw command line, so a pattern spanning pipes/operators/redirects
  (`curl … | sh`) fires. `lint()` honors its `trigger=` and `lang` in string mode.
- The SessionEnd reviewer now fires on interactive session ends. It gated on
  `reason == "prompt_input_exit"`, but that reason is the interactive quit (Ctrl+C/Ctrl+D at
  the prompt) — a headless `claude -p` run emits `reason: "other"` — so the guard was inverted
  and never ran for real sessions. It now skips headless/SDK runs by `CLAUDE_CODE_ENTRYPOINT`
  (the `sdk-*` family) instead.

## [4.5.0] - 2026-07-02

### Added
- Reviewer health ledger: the detached session reviewer records every run's outcome in a
  `spawn_runs` table (single write codepath, `ReviewStore.record_spawn_run`), and
  `capt-hook status` opens with a health line — red `REVIEWER FAILING` with the
  consecutive-crash count and error, a dim `reviewer ok` one-liner, or a yellow staleness
  warning when the reviewer hasn't run in over a week. The recorder re-raises, so crashes
  still land in `spawn.log` with a full traceback.

### Fixed
- Builtin pack hooks now record real module-qualified names (`general.docs:nudge_…`)
  instead of `<frozen importlib:…`: the caller frame walk stops at `captain_hook/packs/`
  modules instead of skipping them with the rest of the framework, and
  `package_aware_stem` pack-qualifies their stems.
- Misfire complaints about pack hooks resolve to the pack source
  (`captain_hook/packs/<pack>/<module>.py`) and are recorded under the captain-hook repo
  key, so their fix PRs open upstream where the hook lives; kinds with a non-module
  prefix (the legacy frozen-importlib rows) resolve to nothing instead of a fabricated
  `.claude/hooks` path.

## [4.4.0] - 2026-07-02

### Added
- `ToolInput(field, pattern)` condition: a multiline regex over one top-level field of
  any tool's raw input. False for non-tool events, a missing field, or a non-string value.
- `WorkflowScript(pattern=…)` / `WorkflowScript(model=…)` condition over a `Workflow`
  tool's inline `script`, or — when only `script_path` is set — that file's contents:
  `pattern` is a multiline regex over the raw source; `model` matches the model names
  pinned in the script's `agent()` opts (`model: 'haiku'`) without hand-written
  quote-aware regex. A missing or unreadable path, or a file over ~1 MiB, never matches
  and never raises.
- `set_tool_input(field, value, *, tool, only_if=(), skip_if=(), note=None)` primitive: a
  declarative `PreToolUse` rewrite that fills a *missing* top-level input field with `value`
  and allows the tool, never clobbering a field that is already present.
- General-pack model-routing hooks (`general/models.py`): block a subagent explicitly pinned
  to haiku unless its prompt is a single-fact mechanical step, auto-upgrade the `Explore` and
  `claude-code-guide` recon agents from the silent haiku default to sonnet, and warn on
  workflow scripts that pin steps to haiku. General pack bumped to 0.2.0.
- Testing `Input` gains `model` and `tool_input` (a verbatim raw-input escape hatch), so inline
  tests can express an Agent/Task call's model and a Workflow script.

### Changed
- Require cc-transcript 7.1.0 for its typed `WorkflowCall`.

## [4.3.0] - 2026-07-01

### Added
- `DurableState`, the cross-session counterpart to `SessionStore`. It is typed and
  scope-keyed, defaulting to project scope and opting into a machine-global file via
  the `scope=` class kwarg, with a `filelock`-guarded `mutate()` context manager so
  concurrent cross-session writers never lose an update.
- `Deque[maxlen]`, a bounded-deque field type whose cap survives JSON round-trips
  and auto-evicts the oldest item on append. Declare a field as `Deque[256]` for a
  `deque[str]` capped at 256, or `Deque[int, 256]` to set the element type.
- A `durable-guard` example hook that warns once per project across sessions.

## [4.2.0] - 2026-06-24

### Added
- `SessionStore.once(key)` and `unseen(keys)` give hook authors scoped,
  session-persisted dedup, replacing the roll-your-own load-set/check/append/save
  pattern. `unseen` records the whole fresh subset in one write, so a batch is never
  partially marked.
- `HookContext.diff(commit=REF)` returns the diff a commit introduced, via a
  root-safe `git show`.

### Changed
- `HookContext.diff()`'s git fallback is now truncated to roughly the token budget.
  It was previously unbounded, which regressed in jj-colocated repos where `ccx`
  returns a hunkless symbol outline.

## [4.1.0] - 2026-06-24

### Added
- `HookContext.diff()` returns a compact working-tree diff, preferring cc-context's
  token-budgeted `ccx diff` and falling back to a plain `git diff` when `ccx` is
  absent. It takes a `source` (`"uncommitted"`, `"staged"`, or any ref), an optional
  `scope` path, and a token `budget`.
- A `diff=` flag on `call_llm`, `llm_gate`, `llm_nudge`, `llm_evaluate`, and
  `prompt_check` attaches that diff to the prompt as a `<diff>` block, so a review
  hook is grounded in the real change instead of reconstructing it from the transcript.
- A `diff-review` example hook demonstrating `llm_gate(diff=True)`.

### Changed
- `transcript=True` on the LLM primitives now sends a recent-event window (15 events)
  rather than the whole session, since the new `<diff>` block already carries the
  full change. Pass `transcript="full"` for the entire history or `transcript=N` to
  size the window in events.
- The general pack's review gate is now an `llm_gate(diff=True)` that reviews the
  compact diff and blocks only on a concrete correctness bug or STYLEGUIDE violation,
  replacing the deterministic gate that fired on any source edit.

## [3.21.0] - 2026-06-23

### Changed
- Adopts cc-transcript `>=6,<7` and its new declarative mining API. The review
  scanner now drives detection through a single spec-based entry point —
  `mine(events, REVIEWER_MINING_SPEC)` over a `MiningSpec(review=ReviewSpec(...))` —
  replacing the six removed `iter_*_signals` per-detector iterators. The three
  review formats move onto the spec: `conductor-finding` becomes a Rust-portable
  `RegexReviewFormat`, while `superset-inline` and `conductor-workstream` stay
  `CallableReviewFormat` escape hatches (lookahead / multi-pass). The FIX-mode
  hook-complaint detector switches from the renamed `adjust` to `bump`, sourced
  with `CONFIDENCE_STEP` from `cc_transcript.mining.spec`. Mined output (formats,
  surfaces, detectors, confidence/reason tuples) is unchanged.

## [3.20.0] - 2026-06-23

### Added
- The `steering` pack gains a band-aid-plan nudge: an `llm_nudge` on `ExitPlanMode`
  (`PostToolUse`, `max_fires=1`, agent mode) that flags a plan-mode plan treating the
  symptom instead of removing the root cause, and points the agent back at a
  first-principles fix.

### Changed
- `HookContext.call_llm(transcript=True)` now wraps the rendered transcript in a
  `<transcript path="…">` tag (new `HookContext.transcript_block`), so an agent-mode call
  can read the untruncated transcript past the `Budget()` clip — e.g. a full `ExitPlanMode`
  plan. Benefits every `llm_nudge`/`llm_gate`/`call_llm(transcript=True)` agent call.
- The `steering` pack now registers its nudges directly in `steering.py`, matching the
  `general`/`python`/`go` packs (no `lib.py` split).

### Removed
- `captain_hook.packs.steering` no longer re-exports `pre_existing_nudge`,
  `trivial_type_nudge`, `TypeCheckerContext`, or the signal constants — these were
  pack-internal building blocks. Copy the condition into a local hook if you need it.

## [3.19.0] - 2026-06-23

### Changed
- The session reviewer now keys a repo solely by its git `origin` remote. The
  git-common-dir path fallback in `resolve_repo_key` is removed, so `capt-hook review
  enable` (and the run-time scan) reject a repo with no `origin` — the reviewer opens PRs
  against `origin`, so an origin-less repo can never be watched. This prevents the stale
  path-form repo keys that silently read as unwatched once a remote was added later.

## [3.17.0] - 2026-06-22

### Changed
- The `python` pack's `NoUnderscorePrefixes` rule now also warns on leading-underscore
  module filenames (e.g. `_common.py`), not only underscore-prefixed classes and constants.
  Style rules can read the post-edit file path via the new required `Change.path` field.
- Requires cc-transcript `>=5,<6` (the 5.0.0 review-scan API); the review scanner passes
  the now-required `surfaces`/`structured_formats` to `iter_review_comment_signals`.
- Track spawnllm 0.4.0: requires `spawnllm>=0.4.0`, adopting its `RunSpec`/`run` invocation API.

## [3.16.0] - 2026-06-22

### Removed
- The `python` pack no longer blocks manual `ruff`, and the `go` pack no longer blocks
  manual `gofumpt`/`golangci-lint`. Running the formatters by hand is fine and encouraged —
  the prek commit hook owns mechanical lint. The packs' `toolchain` hooks keep their
  missing-dependency (`uv sync`) and `go.mod` nudges.

## [3.15.0] - 2026-06-22

### Added
- A `Pattern` condition matches the edit's new content against an
  [ast-grep](https://ast-grep.github.io) structural pattern, the cross-language
  counterpart to `Content`'s regex — `Pattern("os.system($CMD)")` matches the call
  whatever its argument, ignoring strings and comments. The language is inferred from the
  file extension and overridable with `lang=`.
- `ast_grep_rule` and `ast_grep_diff_rule` build `styleguide()` rules from inline ast-grep
  patterns, reusing the existing change-scoping, diffing, and message formatting. They reach
  languages the Python-AST `matchers` can't, while Python naming, ordering, and scope checks
  stay on `StyleRule`/`matchers`.
- `rewrite_code` rewrites edited code structurally before it lands: every ast-grep pattern
  match in an Edit, Write, MultiEdit, or NotebookEdit is replaced through an ast-grep fix
  template — `rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)")`.
- `rewrite_command`'s `(pattern, replace)` shorthand is now polymorphic: a pattern carrying
  an ast-grep metavariable (`$NAME` / `$$$NAME`) rewrites the command structurally over
  tree-sitter-bash, while a plain pattern stays a `re.sub`. `CommandLine` gains `matches` and
  `rewrite` for structural ops on a parsed command line. Shell scripts (`.sh`, `.bash`) are
  recognized as the `bash` language, so `Pattern`, `lint`, and `rewrite_code` reach them too.
- `lint` gains an ast-grep mode: `lint(pattern="console.log($$$)", message="…", lang="ts")`
  flags each structural match by line, with the file guard following `lang`.
- `ast-grep-py` is a new dependency, used in-process for all structural matching and rewriting.

### Changed
- `@workflow_state` models now subclass `WorkflowState` instead of the decorator grafting
  `load`/`save`/`reset` onto the class. The new base, exported from `captain_hook`, carries the
  three event-driven helpers typed with `Self`; keep pairing it with `@workflow_state("name")`.
  Migrate by changing `class ReviewState(BaseModel)` to `class ReviewState(WorkflowState)`.
- Every `styleguide` rule shares one `check(self, change: Change)` signature. The new `Change`
  context, exported from `captain_hook.style`, carries the pre- and post-edit source and the
  lazily parsed `tree`/`pre_tree`, replacing the separate `check(tree)`, `check(pre, post)`, and
  ast-grep `check_source`/`check_diff` methods. Override `check` and read `change.tree`,
  `change.pre_tree`, `change.source`, or `change.pre`.

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

[Unreleased]: https://github.com/yasyf/captain-hook/compare/v8.9.0...HEAD
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
