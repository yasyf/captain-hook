# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`system_message=` shows a hook's text to the user.** `evt.allow`,
  `evt.warn`, `evt.context` and `evt.block` take `system_message=`, carried on
  `HookResult.system_message` and rendered as Claude Code's top-level
  `systemMessage` beside the event's usual envelope. A `Stop` that allows
  renders `{"systemMessage": ...}` on its own. Several hooks' messages join in
  registration order, and `PreCompact`, whose stdout is plain text, drops them.
- **Inline tests seed session state and assert `system_message`.**
  `Input(state=[ReviewState(intent="ship")])` saves each model into a real
  temporary session directory, so `ReviewState.load(evt)` and `evt.ctx.s` read
  it in the handler, alongside any `seen=` keys. `Allow`, `Warn` and `Block`
  take a `system_message=` regex, and `Allow(system_message=...)` requires an
  allow result.
- **Inline tests reach session-aware `Stop` handlers.** `Input(session_id=...)`
  sets the payload's `session_id`, and `Input(transcript=<path>)` now also sets
  `evt.transcript_path` on every event, not only tool events. A
  `FileFixture`, including `home=True` and its `$HOME` swap, now materializes
  on every event too.
- **Inline tests run in a hermetic request environment.** Each test is bound
  as its own request whose forwarded environment is exactly `Input(env={...})`,
  empty by default, so `reqenv.getenv` and `reqenv.env_map()` never see the
  runner's `CLAUDE_*`, `ORCA_*` or other per-request variables. A test run in
  an Orca terminal can no longer reach that terminal through
  `ORCA_TERMINAL_HANDLE`.
- **`Input(agent_id=..., agent_type=...)` reach every event's payload.**
  `Stop`, `PreCompact`, `SessionEnd` and `UserPromptSubmit` tests dropped them,
  so a `skip_if=[FromSubagent()]` guard on those events could not be tested.

### Changed

- **Hooks see the `ORCA_*` environment.** The client forwards every `ORCA_`
  variable with each request, as it does `CLAUDE_*`, so a hook running in an Orca
  terminal can read `ORCA_TERMINAL_HANDLE` through `reqenv.getenv` and pass it to
  `orca terminal send --terminal`. Workers no longer inherit the daemon's own
  `ORCA_*` values.
- **A crashing handler fails its inline test.** `run_inline_tests` lets a
  handler's exception propagate instead of reading it as no result, so the
  test reports an error whatever it expected. Before, a crash satisfied
  `Allow()` and `Ask()` and hid the bug. Live dispatch still logs the
  exception, records a fault and carries on.
- **`RewritingExistingPlan` allows a rewrite once the plan is archived.** A
  `Write` over a plan already written this session no longer matches when a
  sibling `<stem>.*.md` holds the plan's current bytes, such as
  `p.2026-09-24-1530-pre-compact.md` beside `p.md`. The archive keeps what the
  rewrite replaces.

### Fixed

- **`PreCompact` hooks feed the compaction instead of failing it.** Claude Code
  has no `hookSpecificOutput` for `PreCompact` and appends each successful
  hook's raw stdout to the compaction's custom instructions, so the
  `additionalContext` envelope a warn rendered failed schema validation. A warn,
  context, or allow message now prints as plain text, and a block renders
  `{"decision": "block"}`, which cancels the compaction.

### Removed

- **The stray `captain_hook/packs/general/plans.py`.** Nothing loaded it; the
  plan-rewrite guard lives in the `general` builtin pack.

## [12.55.0] - 2026-09-24

### Changed

- **`PendingToolCall` renders under the gate's budget.** 12.54.0 rendered the
  pending call unclipped, so every `PreToolUse` gate carried whole Write and Edit
  payloads into its `max_context`. The block now clips under the primitive's
  `budget=`, and under cc-transcript's default `Budget()` when the hook passes
  none, the 1,500 tool characters 12.53 clipped to. `PendingToolCall(budget=)`
  pins one explicitly; `with_defaults` fills a `None` from the gate.
- **cc-transcript 14.24.0.** A `transcript=` window renders every message queued
  mid-turn under who sent it: `user:` for the user's own, as before, and now
  `notification:` for a task notification, `peer:` for another agent or session and
  `channel:` for an MCP channel event, so a judge can see a condition the user set
  being met by a background task.

## [12.54.0] - 2026-09-24

### Added

- **`budget=` sets the transcript render budget of an LLM judge.** `llm_gate`,
  `llm_nudge`, `llm_evaluate`, `evt.llm`, `HookContext.call_llm`,
  `transcript_text` and `transcript_block` take a cc-transcript `Budget`, now
  exported from `captain_hook`, so a judge that compares a pending call against an
  approved preview can keep both in full instead of the default 1,500 tool
  characters. The default budget is unchanged.

### Changed

- **`PendingToolCall` renders the call in full.** The primitive's `max_context` is
  its one clip. Before, the block clipped the call to the default 1,500 tool
  characters first, so a long pending Slack message and an edited one with the same
  prefix rendered identically.
- **cc-transcript 14.23.0.** With `tool_results=True`, a result in a `transcript=`
  window renders as a `result:` or `failed:` head naming its tool, then its output on
  `> ` lines, so no line of tool output can read as a `user:` or `user answered:` line.
  Calls batched in one assistant message and their results carry `call i/n` and
  `result i/n` ordinals.

## [12.53.0] - 2026-09-24

### Added

- **LLM judges see the pending tool call.** `PendingToolCall` renders the event's
  tool call as a `<pending_tool_call>` block on `PreToolUse` and
  `PermissionRequest`, and nothing elsewhere. It joins `BeforeEdit` and
  `AfterEdit` in the default contexts of every LLM primitive. Claude Code writes
  the assistant entry to the transcript about three seconds after `PreToolUse`
  fires, so a `transcript=` judge could decide on a call it could not see.
- **`tool_results=True` renders tool results in the `transcript=` window.**
  `llm_gate`, `llm_nudge`, `llm_evaluate`, `evt.llm` and `HookContext.call_llm`
  take `tool_results`. When it is set, each finished call in the window is
  followed by its `result:`, or `failed:` when it errored or a hook blocked it.
  The default window is unchanged.

### Changed

- **cc-transcript 14.22.0.** AskUserQuestion answers in a `transcript=` window
  render as `user answered: <question> -> <label>`, followed by the chosen
  option's description, the selected preview and any notes.

## [12.52.0] - 2026-09-23

### Changed

- **`transcript=` windows count messages, not events.** `HookContext.transcript_text`
  and `transcript_block` window with cc-transcript's `Session.recent_messages`, so
  a window of N spans the last N user, assistant and queued-command events. Hook,
  reminder and other attachments between them ride along uncounted. Before, they
  filled the window: right before one Slack send the default 15-event window
  rendered empty. cc-transcript 14.20 also renders messages the user typed
  mid-turn and AskUserQuestion answers, so an LLM judge reading `transcript=` now
  sees both.

### Fixed

- **A blocking LLM gate on a tool event judges every call.** After an `llm_gate`
  on `PreToolUse` blocked once, every later call in the same turn skipped the
  judge and went through, so a retried call bypassed the gate. `llm_evaluate`
  gains `once_per_turn`, and a blocking primitive on a tool event passes
  `False`. Gates on `Stop` and `SubagentStop` keep one block per turn so they
  cannot loop, and `llm_nudge` still fires once per turn.

## [12.51.1] - 2026-09-23

### Fixed

- **The prose-question gate stands down while a `cc-present` board is live.**
  It could block prose that sent the user to a decision already on the board.
  `UsedSkill("present", scope="session")` and a `RanCommand` match for
  `cc-present start` now join `skip_if`, including commands run by subagents.
  The classifier prompt also exempts prose that refers the user to the live
  board, so the same decision needn't go through `AskUserQuestion` or
  `ExitPlanMode` as well.

- **A negated plan-mode request no longer trips the stop-and-replan guard.**
  The guard blocked a plan write after "update the plan again ... (dont enter
  plan mode)" because the clause still matched the negated verb.
  `directs_plan_mode` now counts an enter-plan-mode verb only when it has no
  direct `neg` child. It keeps `dep_related` for the link to the mode noun
  without borrowing negation from a sibling verb: "Re-enter plan mode, don't
  do any more work." still blocks, while "don't go back to plan mode, just fix
  it" allows the edit.

## [12.51.0] - 2026-09-21

### Changed

- **`block_command` with a token list now compares argv, which narrows the
  block it builds.** The tokens were joined into a regex and matched against
  the raw command line, so `block_command(["git", "stash"])` fired on
  `echo git stash`, on a heredoc quoting the phrase, and on
  `git commit -m "git stash"`. A token list lowers to `Runs` and a string
  still compiles to a `Command` regex: tokens mean structure, a string means a
  pattern. `git stash`, `git stash pop` and `a && git stash` still block. One
  carve-out keeps the regex — a list holding `"*"`, where `"*"` stands for a
  word that must be present, and `Runs` matching an argv prefix would widen
  the block onto the bare command the form deliberately lets through. An empty
  token list is refused rather than lowered, because it used to compile to
  `Command("")` and match everything while `Runs()` matches nothing.

### Added

- **`RanCommand` takes a `Regex(...)` in place of its argv tokens.** One
  `RanCommand(Regex(r"\bpytest\b"))` covers `pytest`, `uv run pytest` and
  `poetry run pytest`, where the literal form needs an entry per launcher and
  silently misses the one the author forgot. Opting in through a call rather
  than sniffing the string keeps a literal token holding metacharacters —
  `a.out`, `c++` — matching literally.

- **Constructing a `Command` whose pattern is a plain command-name prefix
  warns with the `Runs` spelling to use.** `Command` searches raw text, so a
  command-name regex fires on any command that merely mentions the phrase; one
  such hook hard-blocked three read-only commands in a single session. The
  suggestion is bounded and skips an end-anchored pattern, which `Runs` would
  widen rather than sharpen.

### Documentation

- The guides, cheatsheet and `Command` docstring teach `Runs` for command
  matching instead of the regex, and four `RanCommand(r"\bpytest\b")`
  examples are corrected — `RanCommand` took argv tokens, so that regex was a
  literal token that could never match, and on a gate's `skip_if` it was an
  exemption that silently never fired.

## [12.50.2] - 2026-09-21

### Fixed

- **The plan-mode hook no longer gags a subagent with its own brief.**
  The hook read prose, so a lane whose task description quoted the words it
  was sent to fix, or merely said verification agents must not outnumber the
  agents doing the work, had every `Edit` and `Write` denied for the lane's
  whole life. `FromSubagent()` joins `skip_if`, which reads the payload's
  `agent_id` rather than any wording, so a brief can never stand in for the
  user. The trigger also separates the two clauses it had conflated: a
  stop-work instruction now blocks only alongside a mention of a plan. A
  main-agent turn that does tell you to go back to plan mode still blocks.

### Documentation

- `UserSaid` says that its patterns are OR-ed, so `UserSaid(a, b)` is a
  disjunction rather than a pair of requirements. Patterns that must both
  hold belong in separate conditions under an `And`.

## [12.50.1] - 2026-09-21

### Fixed

- **The `git stash` block no longer refuses the read-only and tagged forms.**
  The condition matched an argv prefix, so `git stash list`, `git stash show`
  and the documented `git stash push -u -m "<tag>"` / `apply <sha>` / `drop <n>`
  pattern were blocked alongside the dangerous ones. Across eleven sessions the
  hook fired on 109 of 116 calls, and agents that could not use the tagged
  pattern either reinvented a WIP-commit workaround instead. Bare `git stash`,
  `pop`, an untagged `push`, and `clear` still block, and the message now walks
  the tagged pattern rather than only refusing.

## [12.50.0] - 2026-09-20

### Changed

- **Fresh installs reclaim stale Python environments.** Every captain-hook
  release left its `~/.daemonkit/tools/capt-hook/<version>/` environment behind
  indefinitely; one host had accumulated 49 environments taking 14 GB. The
  generated `captain_hook/scripts/install-binary.sh` and `install-mcp.sh` were
  re-rendered from cc-skills' `binrun-shim` fragment (cc-skills#52), moving the
  pinned runner from binrun v0.7.0 to v0.8.0 (yasyf/binrun#9) through
  `RUNNER_TAG` and all four `RUNNER_SHA_*` digests.

  After a resolve installs a new environment, binrun now prunes that
  distribution's environments beyond the newest three. It skips any environment
  referenced in the argv of a live process owned by the same uid, using
  `KERN_PROCARGS2` on Darwin and `/proc/<pid>/cmdline` on Linux. `binrun -- gc`
  uses the same guard. A hook invocation that bootstraps a fresh environment
  now reclaims stale ones in the same run; environments still serving a running
  worker are never removed.

## [12.49.0] - 2026-09-20

### Changed

- **Concurrent hooks share one transcript load.** The host now derives stable
  worker affinity from each request's transcript identity, with separate agent
  lanes and a session fallback. Events for one transcript reach the same worker
  while separate transcripts still spread across the pool. Inside that worker,
  cache misses and classifier lifts are single-flight. A burst of hooks over one
  transcript therefore performs one parse and lift instead of duplicating that
  work and retained memory across the worker shards.
- **Review scans reuse their decision ledger and leave quiet transcripts
  alone.** One decision-log session now covers the scan batch instead of
  reopening the database for every transcript. When mining yields no signals,
  the scan does not read the transcript a second time to build candidate
  context. A scan with no changed paths returns before opening the ledger.
- **The review queue filters before loading context.** SQLite selects the
  eligible event IDs at each prompt version, removes junk-triaged create events,
  and preserves queue order before the larger context rows are fetched in
  bounded pages. The queue no longer loads and sorts every candidate's context
  in Python before discarding most of it.

## [12.48.0] - 2026-09-20

### Removed

- **The maturin rebuild nudge.** It fired on `uv sync` whenever the working
  directory held both a `Cargo.toml` and a `pyproject.toml`, asking for
  `--reinstall-package <name>` instead. The footgun is real — `uv sync` does not
  rebuild a maturin native extension on a Rust-only change, so the stale binary
  keeps running — but the nudge fired on nearly every sync in such a repository,
  and most are not Rust-only. A nudge that is wrong most of the times it speaks
  trains the reader to skip it.

  Its skip test was also defective, which raised the firing rate rather than
  lowering it: `call.flags` holds raw option tokens, so
  `--reinstall-package=name` was a single token the membership test missed, and
  the nudge fired at a user who had already complied. Removing the hook was
  chosen over narrowing that test, since the firing rate was the complaint.

## [12.47.0] - 2026-09-19

### Fixed

- **The teammate approval gate missed most destructive pushes.** It detected a
  dangerous push with a regex alternation over `call.flags`, omitting `-d`,
  git's short form of `--delete`, so `git push -d origin main` from a teammate
  subagent under skip-permissions returned `Allow(explicit=True)` and deleted
  the remote branch with no dialog. Nine spellings were approved on the
  previous release: `-d`, `origin main -d`, `--prune`, `--mirror`, `-uf`, and
  the refspec deletions `+main`, `+HEAD:main`, `:main`, `:refs/heads/main`. A
  `GIT_PUSH` `CommandSchema` now binds the flags by name, so every alias and
  attached spelling is covered by declaration. Because this is an allow/deny
  boundary it fails closed: when `Arguments.complete` is False — an unknown
  option, a missing or ill-typed value, a command substitution — the command is
  treated as dangerous and the human decides.
- **`CommandSchema.bind` accepts attached short values.** `-oVAL` now binds for
  options declared with a value, guarded so `FIND`'s find-style words
  (`-fprint`, `-flags`) do not mis-split. Without this an unknown `-oci.skip`
  ended binding and hid a following `--force`.
- **Enforcement hooks stopped enforcing after their first fire.** `gate()`
  resolves to `nudge(block=True)`, whose handler opens `if block and
  fired_this_turn(evt): return None`, and `PrimitiveState.last_fired_at` is
  session-global across every nudge, gate and LLM hook. So the task gate and
  the Go and Python test gates were one-shot per turn, and any other primitive
  firing that turn silenced them outright — an agent could end a turn with open
  tasks, be blocked, reply "acknowledged", end the turn again and stop with the
  tasks still open. All three are now `hook(..., block=True)`.
- **`find` rewriting missed shapes it was written to catch.** The argv shape was
  hard-coded to `[root] [-type f] -i?name PAT`, so a trailing `-type f` or a
  leading `-L` let a whole-volume walk through untouched. Rebuilt on
  `CommandSchema` with `options_end_operands`, `Operand` and `PathsMatch`. Note
  `-H` is accepted but not re-emitted, so `find -H` rewrites without symlink
  following.
- **`UsedSkill` stood down for one turn instead of the session.** The docs and
  prompts nudges left it at its turn-scoped default while their fire cap is per
  session, so a second turn re-nudged the agent to consult a skill it had
  already read. `docs.py`'s `FilePath("docs/**")` could never match, since
  `File.matches` needs a `**/` anchor against the absolute paths tool inputs
  carry.

### Changed

- Conditions that were hand-rolled now use the built-in that expresses them:
  `FromSubagent()` in the model nudges, the `Waiting()` the primitives already
  add on blocking Stop hooks, and `UserSaid(scope="session")` in graphite's
  `ReviewPassRan`. No behaviour change.

## [12.46.1] - 2026-09-19

### Fixed

- **The stop-and-replan guard could not be satisfied.** The guard blocks edit
  tools after the user tells the agent to stop and re-plan, and cleared on
  `UsedTool("EnterPlanMode")`. A session launched with `permission_mode=plan`
  never calls that tool, and neither does one whose plan was presented with
  `ExitPlanMode` alone, so the skip was unreachable and every edit stayed
  blocked with no way out: the agent could not comply, since re-entering plan
  mode is wrong once the plan is approved, and had to stop and ask. `skip_if`
  is now `[InPlanMode(), UsedTool("ExitPlanMode")]`, reusing the condition that
  already reads `permission_mode` and balances `EnterPlanMode` against
  `ExitPlanMode`. The bare stop-work clause additionally requires the prompt to
  mention a plan, so review feedback that stops one specific thing no longer
  trips a global edit block, and the message names the actual remedy.
- **The review-routing nudges rejected astra for synthesis.** Their rubrics
  told the judges that synthesis/accept-reject over findings belongs on opus,
  so a synthesis stage or spawn routed to gpt-6-astra read as a misroute. Opus
  stays the default; astra at `xhigh` through `codex:codex-wrapper` is an
  equally accepted route and is no longer a finding. Nine sites across both
  review-routing rubrics, the implementation-spawn rubric, and the two hook
  messages. Design/architecture review and hard planning are unchanged.

## [12.46.0] - 2026-09-19

### Fixed

- **The deferral gate no longer fires on the turn its own prompt exempts.** Its
  judge is told not to fire when a turn ends in a question to the user about the
  blocker. It fired on one anyway and cited that question as its evidence. The
  agent then abandoned the pending question and began unrequested work. A closing
  question is now decisive in the discriminator. The deterministic veto recognizes
  `want me to`, `shall I`, `which option/approach/one`, and
  `would you prefer/rather/like` only as a closing question — a question mark, an
  optional label lead-in, then bullet or numbered lines to the end of the
  message. Wording such as "Compiler options: `--strict`" no longer silences a
  genuine silent downgrade.
- **Graphite hooks judge the repository the command targets.** `GraphiteActive`
  read the session's cwd, so `cd /other/repo && git push` inherited the session
  repository's policy and a plain-git push was told to use `gt`. `GraphiteRuns`
  replaces it and judges the verb and the repository on the same call, reading
  only git's leading global options as directory hops and tracking cwd and
  `--git-dir` separately.
- **`Runs` sees through git's leading global options.** `Runs("git", "push")`
  never matched `git -C x push` because it compared a raw argv prefix, so the
  `-C` and `--git-dir` cases could not fire at all. `Call.verb_argv` strips
  leading global options and their registered values, and stops at `-h`,
  `--help`, and `--version`, so `git --help stash` is not read as a mutation.
- **The pre-submit gate drops its approval demand.** It asked whether the user
  had approved publishing, which standing instructions grant durably. The
  required review pass and the never-draft rule remain.
- **Fix PRs get their own open-PR pool.** `crosses_thresholds` applied one cap
  to both kinds, so two unmerged create PRs froze every fix candidate in a
  repository regardless of its evidence. `max_open_prs_fix` supplies the fix
  cap, open targets count per `(repo, kind)`, and `slots --kind create|fix`
  reports each pool.
- **A complaint that names a hook in plain words reaches the fix lane.**
  Attribution required the exact "the `<name>` hook" form or a message
  fingerprint. Marker extraction now reads user turns, tolerates typos, and
  attributes to a hook the complaint itself binds, or to a unique ledger row
  whose fire message shares at least two content words. A complaint about a hook
  that never fired in the session is dropped, since no ledger row identifies it.
- **A closed pull request the Graphite merge queue landed records as accepted.**
  The queue lands its own rebased commit and closes the pull request through the
  API, so GitHub reports `CLOSED` with no merge metadata. Every queue merge was
  therefore stored as a rejection, inverting the lane's learning on every
  Graphite repository. A closed pull request is now resolved by evidence: the
  close event's commit, else the base branch's history around `closedAt` carrying
  a commit that cites the number. An unreadable or ambiguous window leaves the
  candidate untouched.
- **Citations in the performance pack name the right section.** The two
  pipelining messages cited `§ Plan Execution & Orchestration (Speculate while
  you verify)`; they now read `§ Speculate Across Gates` with the older heading
  named for repos not yet re-bootstrapped.

### Added

- **Inline tests can seed session state and stub subprocess output.** Every
  `Input` was built without a session directory, so deduplication always looked
  fresh, and nothing stubbed a subprocess, so a hook shelling out to `gh` could
  not reach its fire path. Prose-matching conditions were therefore easy to test
  while live-state conditions could not be tested at all. `Input.seen` seeds a real
  temporary session store and `Input.commands` supplies stdout by argv prefix,
  longest match winning, with unmatched commands running for real.

### Changed

- **The inline-single-use-constants guard exempts values a name documents.** It
  fired on 347 constants across 125 files, warning on code an edit had not
  touched. Multi-line strings, strings over 100 characters, and collection
  literals spanning three lines or more are now exempt, recursing through
  f-strings and string concatenation. The remaining single-use constants are
  inlined at their call sites.

## [12.45.0] - 2026-09-19

### Fixed

- **Model-routing messages and rubrics follow the current table.** The general
  pack's implementation, inline-edit, browser, and review nudges replace retired
  `gpt-5.6-sol` advice with `gpt-6-astra` at `xhigh` and restore opus as
  the default implementation lane. Individual bounded, decision-light changes
  use opus at `high`; ambiguous, exploratory, decision-dense, large net-new,
  and long-running implementation use `xhigh`. Repetitive bounded sweeps use
  astra at `xhigh`, or sonnet at `xhigh` when they must stay Claude-side,
  and never opus. Shell-heavy execution routes to astra at `xhigh`.
- **Browser work, design review, and findings synthesis route to opus at
  `xhigh`.** Sustained browser automation and QA sweeps run in a delegated
  opus subagent, including an `agent-browser-with-cookies` teammate when the
  site needs the user's login. Code/diff review uses an astra finder and adds a
  refuter only at audit depth. Security review/audit, verification of
  security-sensitive code, and bug diagnosis also use astra at `xhigh`.
  An astra miss escalates to opus at `xhigh`; fable requires an actual opus
  `xhigh` miss first. Missed implementation lanes cross between opus
  `xhigh` and astra `xhigh` before reaching fable.
- **Sensitive implementation no longer qualifies for inline editing.** Auth,
  migrations, concurrency, data-loss risks, crypto, and subtle algorithms go to
  a typed `model='fable'` subagent. Only other small changes or changes bound
  to judgment the agent just exercised retain the inline exception.
- **Unpinned spawns and stages run opus, and citations resolve under either
  heading.** The haiku messages no longer claim that omitting a model inherits
  the session model. The eight replacement messages and five rubrics cite
  `CLAUDE.md § Model Routing` and name `§ Plan Execution & Orchestration`
  for repos not yet re-bootstrapped.

## [12.44.0] - 2026-09-19

### Fixed

- **`prose_spawn_gate` and `prose_workflow_nudge` stopped enforcing a retired
  routing rule.** Both hooks in `captain_hook/builtin_packs/general/hooks/models.py`
  still demanded that prose run on fable, told the caller to pass `model='fable'`,
  and cited `CLAUDE.md § Plan Execution & Orchestration (Models)`, a section that
  no longer exists. The misfire that exposed it: an `Agent(model: opus)` spawn
  whose prompt only orchestrated an incident-retro revision — every sentence of
  prose delegated onward to gpt-6-astra through the codex skill — was blocked
  outright, with a message instructing the caller to pin fable, a model the
  spawn never touched. The rule is now the current one: prose routes to
  gpt-6-astra at `xhigh` through the codex skill, and a subagent or workflow
  `model:` takes only Claude models, so no pin — fable included — routes prose.
  What clears a spawn is the route, not the pin. Both hook messages now cite
  `CLAUDE.md § Model Routing`, and both judge rubrics
  (`prompts/models/prose_spawn_gate.md`, `prose_workflow_nudge.md`) were
  rewritten to match.

### Changed

- **`prose_spawn_gate`'s `skip_if` now matches the route, not a model pin.**
  It gained a `PROSE_CODEX_ROUTE` check on the prompt (the words `codex` or
  `astra`) and widened its agent carve-out to `codex-wrapper|codex:codex-wrapper`,
  so an orchestrator whose prompt hands writing to codex, or a spawn that is
  itself a `codex:codex-wrapper` agent, clears the gate. It lost the
  `model=fable` exemption entirely, so a fable-pinned prose spawn now blocks
  where it previously passed — the one behaviour change a caller may notice.
  Regression coverage sits inline on both hooks: the misfire shape (an opus
  orchestrator whose prompt delegates every sentence to astra) now returns
  `Allow()`, as does a `codex:codex-wrapper` spawn asked to rewrite the README
  quickstart, while a sonnet, opus, haiku, or fable spawn that writes the prose
  itself still blocks. The new tests were verified red against the old
  condition and green against the new.

## [12.43.0] - 2026-09-18

### Changed

- **The `Waiting()` skip on blocking Stop gates is back, now with a
  `guards_waiting` opt-out.** 12.42.0 removed the injection outright. A survey of
  all 14 installed packs says that was too strong: of the seven gate-backed Stop
  hooks, six want the skip and only the general pack's prose-question gate does
  not — its whole subject is the turn that parks on background work instead of
  finishing. `gate`, `nudge(block=True)`, and `llm_gate` take
  `guards_waiting: bool | None`; `None` restores the pre-12.42.0 rule, `False`
  turns the skip off, and `True` opts a non-gate into it. The prose-question gate
  passes `False`. Packs that added `skip_if=[Waiting()]` for 12.42.0 need no
  change — the condition is idempotent — and the one out-of-tree Stop gate in the
  installed set never needed one.

## [12.42.0] - 2026-09-17

### Added

- **An inline test can express a waiting session.** `Input` takes
  `background_tasks`, threaded through `mock_stop_event` and
  `mock_subagent_stop_event` into the `Stop`/`SubagentStop` payload and surfaced
  as `evt.background_tasks`. Without it no test could reproduce a gate that only
  misfires while background work is in flight.

### Changed

- **Claude-backed hook calls now run through the Claude Agent SDK rather than
  the `claude` CLI.** The dependency becomes `spawnllm[sdk]`, which installs
  `claude-agent-sdk` and makes spawnllm's `ClaudeSdkBackend` the first ready
  backend for the `general` specialty. Structured output travels over the SDK's
  typed `json_schema` transport instead of CLI flags and parsed stdout. This is
  latency-neutral — the SDK hosts the same bundled Claude Code binary, so both
  routes pay the same spawn, and a trivial structured call measures 5.11 s
  median before and 4.95 s after. Isolation is unchanged: the SDK run passes
  `setting_sources=[]`, `strict_mcp_config`, and `no-session-persistence`, so it
  still inherits none of the caller's settings, hooks, or MCP servers.
  Authentication still comes from the ambient `/login` session, with no new key.
  The cost is disk: the SDK wheel bundles the Claude Code binary, so each
  installed tool environment grows by about 213 MB on macOS arm64 (an 88 MB
  download that unpacks to a 206 MB executable), and `capt-hook update run`
  keeps one environment per released version.

- **A fixed signal `window` counts scored prose entries, not raw JSONL events.**
  `window=6` meant "the last six transcript events", and tool calls and their
  results are events that carry no prose, so a message followed by a handful of
  reads fell out of scoring range before it could be matched. It now means the
  last six texts a signal could match, scanned backwards with an early exit, so
  intervening tool traffic costs a window nothing. `window="turn"` is unchanged.

- **`gate`, `nudge(block=True)`, and `llm_gate` no longer inject `Waiting()` into
  `skip_if`.** A blocking Stop gate silently skipped whenever a background shell,
  subagent, workflow, monitor, or scheduled wake-up was in flight, and no
  parameter turned that off — so a gate whose whole subject is the turn that
  parks on background work could never fire. `skip_if` is now taken verbatim and
  a gate that wants the guard passes `Waiting()` itself, which every builtin gate
  that wants it now does. Out-of-tree packs relying on the injection must add
  `skip_if=[Waiting()]` to keep the old behaviour.

### Fixed

- **The general pack's prose-question gate fires on the deferral formulas agents
  actually use.** "Still yours to decide: ..." scored zero against the gate's
  signals, so its judge was never consulted and the turn ended on a question the
  user was never asked. The signal set now covers `yours to decide/call/choose`,
  `up to you`, `say the word`, `if you'd rather`, `leave it to you`,
  `needs your sign-off`, `tell me which`, and `your decision/choice/move`, and a
  closing `Want me to ...` offer no longer needs a question mark.

## [12.41.1] - 2026-09-17

### Fixed

- **A killed hook spawn no longer leaves a copy of the OAuth token on disk.**
  spawnllm ran its isolated `claude -p` calls against a temp config directory
  and wrote a `.credentials.json` copy of the token into it for the child to
  read. Every spawn killed before its cleanup left that copy behind, and Claude
  Code migrated each one into a login-Keychain item. One machine carried 345.
  0.13.4 passes the token through `CLAUDE_CODE_OAUTH_TOKEN`, so spawnllm writes
  no copy at all. The floor moves from `>=0.13.2` to `>=0.13.4`: an older
  resolution still carries the leak.

## [12.41.0] - 2026-09-17

### Added

- Command schemas bind typed options and named operands from parsed shell commands.
  Shared path predicates let hooks express rules over those roles; the authoring
  skill and examples now teach this approach.

### Fixed

- The performance pack blocks unbounded `find` searches from filesystem roots,
  home directories, and entire worktree pools. Scoped searches and verified
  depth limits of zero to two remain allowed. Name filters and `head` pipelines
  no longer let agents start these broad traversals.

## [12.40.0] - 2026-09-17

### Fixed

- **A hook the caller's deadline abandoned no longer starves every other
  event in its worker.** Synchronous hooks fanned out onto one 16-thread pool
  per worker, and an abandoned hook kept its thread until it finished, so a few
  slow LLM hooks left every other session's hooks queued until their own
  deadlines passed: over three days one machine logged 53,168 `caller deadline
  is near; skipping this hook` and 32,298 `abandoning this hook's verdict`
  warnings, most of them on plain nudges that never got a thread. Each event
  now fans out onto its own pool, which dies with the event, and a worker-wide
  budget of 64 permits bounds the hook groups in flight across every event a
  worker serves at once. A group returns its permit when it finishes, and the
  dispatcher returns the rest when the reply is settled, so an abandoned hook
  never holds one: the ceiling is 64 hook threads somebody is waiting on, plus
  the abandoned ones still unwinding. Once the envelope is settled, a hook
  still running, whether abandoned or doomed by an earlier block, stops at its
  next checkpoint: before an LLM call, a `call_cli` subprocess, or a transcript
  parse, and between directories of a glob or repository walk.

- **A detached reviewer no longer outlives the host generation that started
  it.** `review spawn` leaves its worker's session so a retiring worker cannot
  kill a review, which also put it beyond the host's shutdown settlement: one
  machine carried 22 live reviewers, several reparented to pid 1 from the
  12.37.0 generation and one at 89% CPU for over four hours. The worker now
  sends the host an `adopt` frame naming the child, and the host records it
  through daemonkit's `Adopt` under its own generation. Shutdown settles the
  reviewer's whole session, the next generation reclaims any record that
  survived, and the host terminates the session once `spawn_deadline_seconds`
  plus five minutes has run, a bound a blocking call inside the child cannot
  outlast. A cold `capt-hook run` has no host and is unchanged.

### Added

- **One `dispatch` line per event in the worker's daemon log.** It carries
  the event, root, client pid, `queue_ms` spent waiting for a request thread,
  `elapsed_ms` in dispatch, and the names of the hooks whose verdicts were
  abandoned, so a stall shows up as numbers rather than as a client timeout.

## [12.39.0] - 2026-09-17

### Changed

- **Deep, subagent-spanning predicates use the incremental transcript path.**
  cc-transcript 14.19.0 makes those hook conditions incremental: a growing
  subagent transcript extends its held lift instead of re-parsing the whole
  file. After a one-line growth, a deep predicate call takes ~13-30 ms by
  sidechain size, against 22-169 ms before. It also stops re-parsing an
  unparseable sidechain on every event.

## [12.38.0] - 2026-09-17

### Changed

- **A worker loads spaCy and WordNet after its first reply.** The first
  event a worker answers starts a background load of both, so a later hook
  that reads them finds them loaded, or waits on the load already running,
  instead of paying ~1.5s itself. The first reply is not slowed: the load
  starts once it has been written.
- **A busy root gets a pool of workers, not one.** The host keeps a pool of
  Python workers per `{root, semantic environment}` key and sends each event to
  the member with the fewest events in flight. A pool starts with one member;
  while its least loaded member is busy and the pool has room, the host starts
  one more in the background, one start at a time, and the event takes the
  member that is already up. The pool bound is `CAPT_HOOK_WORKERS_PER_ROOT` in
  the host's environment, else a quarter of the cores capped at 8, and growth
  only ever takes a free slot under the 64 live workers the host allows across
  every root. Each member is one interpreter, so a root whose sessions used to
  share one core now spreads across the machine. Members idle for 30 minutes
  retire as before; a quiet pool routes to its most recently used member on a
  tie, so the rest go cold. Every session-scoped fact a hook reads —
  `max_fires` counts, `PrimitiveState`, `once` keys, workflow state — already
  lives on disk under a file lock, and the transcript parse cache and ledger
  writers are per-process caches over the same files, so two members of one
  root give the same answers one worker did. Each member writes its own
  `daemon-<build>-<root>-<shard>.log`; the worker reads its shard from
  `CAPT_HOOK_WORKER_SHARD`, which the host sets. `capt-hookd status` rows
  carry a `shard`.
- **An event the worker cannot answer in time is shed at once.** Each pool
  member keeps a smoothed service time, one round trip divided by the events
  sharing the interpreter when it was admitted. When the events already in
  flight on the chosen member, at that service time, outlast what is left of
  the caller's deadline, the host replies with an exit-0 no-verdict at once
  and names the queue on stderr, instead of queueing the event to run after
  the harness has given up on it. The same check runs again where the event
  is dequeued: the host never writes an event whose caller has already gone,
  and the worker answers an event that reaches its thread with less than the
  5 s hook margin left with the same no-verdict reply, instead of loading the
  session and transcript only to skip every hook. Nothing serializes: every
  admitted event still runs as it arrives on the member it was routed to.
- **Warm events stop re-reading the plugin roster.** The daemon checks
  whether its cached hook set is still current on every event, and that check
  read Claude Code's plugin roster and walked each enabled plugin's
  `capt-hook/` tree every time. It now reuses the last read while
  `installed_plugins.json` and the settings files that decide enablement are
  unchanged. Installing, updating, enabling, or disabling a plugin still takes
  effect on the next event. An in-place edit inside an installed plugin's pack
  takes effect within 30 seconds. Edits under `.claude/hooks` still take
  effect on the next event. When the cache expires, concurrent events wait for
  a single re-read or re-walk instead of each running their own, and this now
  covers the language-marker walk too.
- **The worker reuses a lifted transcript `Session` while its file is
  unchanged.** Each cached parse now also holds the `Session` lifted from it,
  one per user classifier. Every request still resolves its own classifier, so
  a request whose classifier differs lifts its own. A lifted `Session` is
  evicted with its parse. On a 44 MB transcript the lift took 97 ms and a
  reuse takes 0.03 ms. The lifted `Session` adds about 0.4x the source size in
  RSS on top of the parse's budgeted entry.
- **A growing transcript extends its lifted `Session` instead of relifting
  it.** Claude Code appends to the transcript between almost every hook event,
  and each append used to relift the whole session. Each cached parse now
  keeps one cc-transcript `ActivityLift` cursor per user classifier, and a
  growth extends those cursors by the appended events alone. A shrink, an
  in-place rewrite, or a parse error still lifts afresh. After an 8-line
  append to a 63 MB transcript, a load took 32 ms and now takes 8.4 ms, most
  of it reading the file and parsing the appended lines; the lift itself takes
  0.02 ms. A cursor adds only its tool-use and result indexes, 0.18 MB on that
  transcript, so the source-byte budget is unchanged. The cc-transcript pin is
  now exactly `14.18.0`, the first release with `ActivityLift`.
- **A growing transcript reads only its appended bytes.** A growth used to
  read the whole file before parsing the new lines. It now seeks to the end of
  the prior parse and reads only up to the size it checked. A read shorter
  than that size, from a file truncated or rewritten in between, falls back to
  a full reparse. The same 8-line append to a 63 MB transcript now loads in
  0.7 ms, down from 9.8 ms.

### Fixed

- **A session with registered codex rollouts no longer rewalks `~/.codex/sessions`
  on every event.** Each registered thread id used to resolve against the whole
  sessions tree whenever an event read the transcript, at 9-14 ms apiece. A
  session with 85 registrations spent about 300 ms of CPU per `PostToolUse`
  there. The worker keeps one index of the newest uncompressed rollout per
  thread id, stamped with the modification time of every directory in the tree.
  An event checks the stamps, 34 directories here (under 1 ms), and rescans only
  when a directory changed. The index resolves exactly
  what a fresh lookup would, including a newer rollout for an id already seen.
- **Each dispatch pass evaluates only its own hooks' conditions.** The
  synchronous pass used to evaluate `only_if`/`skip_if` for `async_=True` hooks
  and then discard them, and the background pass did the same for synchronous
  hooks.
- **A list of `RanCommand` spellings walks the transcript once.** In `skip_if`
  or `Or(...)`, each unbroken run of `RanCommand` conditions with the same
  `subagents` flag is answered in one pass over the session, its sidechains, and
  its attachments, instead of one pass per spelling. Conditions between runs
  still evaluate in order, so a side-effecting condition such as `once` sees the
  same calls it did before. The steering pack's typing nudge lists seven spellings and
  runs on every `PostToolUse`.
- **The general pack's multi-request nudge no longer backtracks quadratically
  on a long prompt.** Its imperative-count signals used a leading lazy `.*?`,
  which made every failed search retry from every offset. A 40 KB prompt with
  only two imperatives took 46 s of CPU on each `UserPromptSubmit`. The
  signals now start at the first imperative. They match the same prompts, and
  the same prompt takes 10 ms.
- **Echo damping skips the entity recognizer when it collects content
  lemmas.** No lemma or part-of-speech rule reads entity types, so the lemma
  sets are unchanged. On a 40 KB prompt the pass drops from 1.0 s to 0.64 s of
  CPU.

- **The host and its Python workers run at default priority.** The host
  LaunchAgent declared no `ProcessType`, so launchd applied its resource
  limits to capt-hookd and every worker it spawned, whose main threads ran at
  utility QoS. Under a busy machine a worker's startup imports took 12s where
  the same interpreter from a terminal took 1s. The agent now declares
  `ProcessType` `Interactive`, which spawned workers inherit.

- **A worker loads spaCy and WordNet once.** Two hooks reaching an unloaded
  NLP resource together could both load it: the loader checked for a stored
  value under its lock, but the value was only stored after the lock was
  released, so a second thread woken in that gap loaded the pipeline again.
  The loaded value is now stored before the lock is released.

- **The worker's transcript parse cache holds a byte budget, not eight
  entries.** Parsed events take about three to four times their source bytes
  in RSS, so eight large transcripts could pin gigabytes in one worker. The
  cache now evicts least-recently-used transcripts once their summed source
  size passes 128 MiB, always keeping the one just parsed.
- **A transcript rewritten mid-read no longer splices old and new events.** The
  worker took the transcript's size and timestamps once, before reading. A file
  rewritten to equal or greater length in between counted as growth, so the
  worker parsed the replacement's bytes after the old consumed offset and
  appended them to the cached events. A growth now rechecks the open file's
  inode, size and timestamps after reading and reparses in full on any
  mismatch. A full reparse caches its result only when the file's stats before
  and after the read agree.
- **A timed-out plain-English rewrite no longer strands a thread.** Each
  finalized `MessageDisplay` built its own one-thread executor and released it
  without joining, so a rewrite that outlived its budget kept its thread alive
  until the Cerebras call returned. Rewrites now share one pool of four
  threads, and a rewrite still queued when its budget runs out is cancelled
  instead of running later.

## [12.37.0] - 2026-09-16

### Fixed

- **An upgrade no longer wedges on the staging slot an aborted install left
  behind.** `package-install` stages the new app into a private slot beside the
  installed one; an install that aborted after that copy but before committing
  the swap left the slot occupied, and every later upgrade then failed with
  `land delivered app: deploy: bundle version mismatch: got "12.34.0" want
  "12.35.0"` — naming a version nobody had asked for, identically on every
  retry, until the slot was deleted by hand. daemonkit v0.31.1 discards a slot
  holding a generation the install is not landing, and keeps adopting one that
  holds the generation it is, so an interrupted install still resumes without
  copying the bundle again.

## [12.36.0] - 2026-09-16

### Added

- **The general pack blocks prose questions to the user at Stop.** The
  `prose_question_to_user` gate fires when a turn ends by putting a decision
  to the user as text ("one decision for you", "let me know which", "should
  I …?") without an `AskUserQuestion` or `ExitPlanMode` call, and tells the
  agent to ask through `AskUserQuestion` in the same turn.

## [12.35.0] - 2026-09-16

### Changed

- **The hook client comes from binrun again.** The plugin registers
  `"${CLAUDE_PLUGIN_ROOT}/bin/hook" run <Event>` once per event, and `bin/hook`
  hands off to binrun with a signed-app descriptor that sets `copy_exec`, so
  the hook runs a cached copy of `capt-hookd` from outside the bundle a deploy
  replaces. The descriptor requires app 12.31.0, so a new plugin on an older
  app gets binrun's `brew upgrade` error rather than a host that cannot serve
  it. Nothing places a client under `~/.daemonkit/bin` any more: the
  `install-client` command, the copy `package-install` made, its publish lock
  and version check, and the copy the MCP server made at startup are gone,
  and so is the shell fallback each hook command carried.

### Upgrading

- A machine whose app is older than 12.31.0 gets binrun's upgrade error on
  every hook until `brew upgrade yasyf/tap/captain-hook` runs. The error exits
  1, so it never blocks a tool call.

## [12.34.0] - 2026-09-15

### Changed

- **The plain-English reply rewrite now lives in the `general` pack.** The
  separate `plain_english` builtin pack is gone; its one `MessageDisplay` hook
  is `general/hooks/plain_english.py`, with the same `CEREBRAS_API_KEY`
  activation and behavior, and `capt-hook pack list` no longer shows a
  `builtin:plain_english` row.

## [12.33.0] - 2026-09-15

### Changed

- **An event's hooks run concurrently.** The matching hooks of a single event
  all start at once on one process-wide 16-thread pool, so an event costs its
  slowest hook rather than the sum of them: five listeners each sleeping 100 ms
  finish in 107 ms where they took 524 ms. Only the starting is concurrent —
  the verdicts fold in registration order, so a block still wins wherever it
  landed, messages still join in the order the hooks were registered, and the
  envelope is the one a hook-at-a-time dispatch would have rendered. A hook is
  suppressed only by a block from a hook registered *ahead* of it, never by
  whichever hook happened to finish first. Registrations that share a state key
  share one `max_fires` counter and one `PrimitiveState`, so they run in
  registration order within their group while the groups run side by side. The
  caller's deadline still gates both ends: a hook does not start once the
  deadline is inside the margin, and a hook still running when the budget runs
  out is abandoned so the reply lands in time. A hook already running when
  another blocks is no longer cancelled — the fold drops its verdict, but it
  has spent its `max_fires` slot. `async_=True` hooks fan out the same way,
  each with its own timeout measured from its own start, on a pool of their
  own so three-minute background work cannot hold the threads a blocking gate
  needs.

## [12.32.0] - 2026-09-15

### Added

- **The `plain_english` builtin pack rewrites replies into plain English.** It
  replaces the `claudish-to-english` plugin and is active in every repo once
  `CEREBRAS_API_KEY` is set. On Claude Code's `MessageDisplay` event it hides
  each streamed chunk of an assistant reply, then shows a rewrite of the whole
  reply from Cerebras `qwen-3.8-27b` on the last chunk. The rewrite keeps code
  blocks, names, and numbers, and breaks long sentences into short ones. A
  reply with under 200 characters of prose outside code blocks shows as
  written. A failed rewrite shows the original reply and records a fault.

### Fixed

- **An install no longer fails because the app it just quit is still being
  reaped.** `capt-hook package install` stops the installed app generation
  before it supersedes the bundle, and read a generation that had already
  exited as a foreign one running from an unexpected path: LaunchServices goes
  on publishing an application for another 2-47 ms after its process exits,
  tearing the record's `bundleURL` down before it flips `isTerminated`, and the
  identity check treated that empty URL as a mismatch. The stop polls every
  50 ms, so any poll landing in that window failed the install. A generation
  whose process is gone is now absent, which is what the stop set out to prove;
  only a live process can be a foreign generation. The failure aborted before
  anything was written, leaving the app and `~/.daemonkit/bin/capt-hookd` on the
  previous build while brew and the plugin had already moved on, so hooks went
  dark until someone re-ran the install.
- **A failed stop or broker ping now says what the child reported.** Both wrap
  the child's exit status together with its stderr, which was previously read
  only on success and discarded on the failure that needed it. An install that
  cannot quiesce the installed app also reports that nothing was applied and
  which path still serves the previous generation.

## [12.31.0] - 2026-09-15

### Changed

- **Every hook is one plain `capt-hookd run <Event>` command.** The plugin
  registers `~/.daemonkit/bin/capt-hookd run <Event>` once per event through
  shell builtins, with no `--async` twin, no bash wrapper, no binrun, and no
  Python shim. When that client is missing, every event but `SessionStart`
  exits 0, so hooks go dark between a plugin update and the app upgrade. For
  `SessionStart` the plugin falls back to the bundle's
  `capt-hookd run SessionStart --async`, which runs the updater that brings
  the app forward. `capt-hook helper install` copies the deployed bundle's
  `capt-hookd` to `~/.daemonkit/bin` after activation, so a running hook never
  executes out of the bundle a deploy replaces. The client reads the event,
  sends one request, and prints the reply. While the host refuses the event
  before dispatch (not listening, starting, draining, or out of session
  slots), the client resends it every 100 ms within its own deadline, so a
  deploy shows up as hook latency instead of a `not installed` error. The
  `capt-hook` MCP server also runs `capt-hookd install-client` at startup, so a
  plugin that updates ahead of a deploy still finds the client.
- **The host runs every event as it arrives.** The per-agent lanes, the
  adaptive pool, the async cap, and deadline shedding are gone, and with them
  the `overloaded` refusal and `CAPT_HOOK_MAX_PARALLEL`. Seven events from one
  agent now run side by side on the worker instead of in series. Each worker
  holds at most 64 outstanding events, abandoned ones included; the next
  event waits for a reply, up to its own deadline.
- **The host looks up its own Python.** Events no longer carry the
  interpreter, the build, or an async flag. `capt-hook helper install`
  installs the `capt-hook` tool env for the app's build before it touches the
  installed app, aborting with the old host running when that install fails.
  The host only looks that env up, failing worker start with the install
  command when it is missing. Publishing `~/.daemonkit/bin/capt-hookd` takes a
  lock and never replaces a newer build with an older one.
- **Any process running as your user may send the host events.** The business
  lane admits the same user instead of three signed identities; only the
  signed host may drain it.
- **Hooks in a development checkout run the installed wheel.** The `hook`
  console script execs `~/.daemonkit/bin/capt-hookd run <Event>` and exits 0
  on `--async`, so `.venv/bin/hook` dispatches to the installed app's tool env
  rather than the checkout's `.venv`.
- **One request per event runs both kinds of hook.** The worker runs an
  event's synchronous hooks, sends the reply, then runs its `async_=True`
  hooks on a small background pool, each with its own 180-second deadline.
  The reviewer and updater dispatch and stale-session cleanup run once per
  event on the same pool. `capt-hook run`
  no longer accepts `--async`. Hook packs need no changes.
- **The plugin roster is read from Claude Code's files.** Discovery reads
  `installed_plugins.json` and the `enabledPlugins` maps of the user,
  project, local, and managed settings files on every discovery. It no
  longer spawns `claude plugin list`, and the roster snapshot, failure and
  outage records, spawn gate, and background refresh are gone. Installs whose
  directory no longer exists are skipped. In a git worktree the local settings
  file comes from the main checkout's root, over any older copy in the worktree,
  as Claude Code reads it. A plugin enabled only through an MDM profile or a
  remote managed policy is not seen.
- **The worker answers the host's hello before importing the runtime.** Logging
  setup, the login-shell `PATH` probe, and the runtime import now run after the
  handshake and before the first event is read, so a cold start no longer
  spends the handshake's readiness budget.
- **Reviewer passes over one repo take turns on a per-repo lock.** `review
  spawn` locks on the repo's origin. A `SessionEnd` review waits for the
  current holder, and a `Stop` sweep exits when the lock is taken. The
  60-second review-run dedupe stamp is gone.

### Removed

- The pre-v0.21 host stop and the restored-abort retry in `package-install`,
  and the vendored bundle digest, now `deploy.BundleDigest`.

### Fixed

- **An LLM call no longer outlives the request that started it.**
  `ctx.call_llm`, and `prompt_check` through it, cut their timeout to the
  seconds left before the caller's deadline.
- **A signal-gated `llm_nudge` or `llm_gate` stops re-judging text it already
  cleared.** A verdict that does not fire now marks the signal text it scored,
  so the same transcript text is not sent to the model again on later tool
  calls.

### Upgrading

- Sessions on plugin 12.28 or older fail every hook until they restart, since
  the client no longer accepts the `run --event … --python … --build …` flag
  form. Sessions on 12.29 through 12.30 keep working, since `run <Event>` is
  still the command, but still run the bundle binary, so a deploy can stop
  their hooks until they restart.

## [12.30.11] - 2026-09-15

### Fixed

- **A hook from a removed project directory no longer switches plugin packs off
  machine-wide.** Discovery ran `claude plugin list` with the removed directory
  as its working directory, so the spawn failed with `FileNotFoundError`, which
  was recorded as a machine-wide roster outage. For the next 15 seconds every
  project served only built-in packs, and scratch sessions whose temp directory
  is removed at `SessionEnd` kept renewing it. The roster is now read from the
  nearest ancestor that still exists.

## [12.30.10] - 2026-09-15

### Fixed

- **A hook whose project directory is gone spawns its worker again.** The
  worker falls back to the system temp directory once its root is deleted,
  but macOS spells `$TMPDIR` with a trailing slash and daemonkit refuses a
  `Cmd.Dir` that is not clean, so the dispatch failed with `Cmd.Dir
  "/var/folders/.../T/" is not absolute and clean`. The fallback is now
  cleaned.

## [12.30.9] - 2026-09-14

### Fixed

- **A hook client whose working directory was removed leaves it for `/`
  before Go's own startup calls `getcwd`.** Orphaned `capt-hookd run
  SessionEnd` clients sat for over ten minutes at 50 s of CPU each, before
  `main` ran, in `$TMPDIR/slop-cop-llm-*` directories slop-cop had already
  removed: package `os` evaluates `initCwd, initCwdErr = Getwd()` at init on
  macOS, and libc's `getcwd` scans the former parent, a `$TMPDIR` of 69,500
  entries, restarting as it changes. `requestCWD` had stopped calling `getcwd`
  in 12.30.1, but nothing of ours runs before the runtime's call. A new
  `cwdguard` package, importing only `syscall` so it initializes ahead of `os`,
  checks the working directory through `F_GETPATH` and an identity stat, moves
  a removed one to `/`, and `requestCWD` then reports `$PWD` as before. A test
  runs the client from a removed directory under `GODEBUG=inittrace=1` and
  fails if `os` ever initializes first.

## [12.30.8] - 2026-09-14

### Fixed

- **Queue waits are estimated from each hook event's own smoothed service
  time.** A `PreCompact` hook was skipped during `/compact` as `2 hooks ahead
  on this session's lane at 19.272s each outlast the 29.87s left`. A lane's
  timing record was dropped with the lane, so the check fell back to the
  host's last completed dispatch, whatever its session or event, and counted
  a nearly finished holder as a full turn. The host now keeps an
  exponentially weighted moving average of service time per event, sums the
  estimates of the dispatches actually queued ahead, and counts a running
  hook only for what its estimate has left.

## [12.30.7] - 2026-09-14

### Fixed

- **`package-install` retries once after an upgrade the incumbent outlived.**
  The first install of 12.30.1 aborted after 50 s because the old `capt-hookd`
  was still in the process table at the kill's settlement deadline. daemonkit
  put the prior generation back in service, and a manual retry a minute later
  installed in 8 s. When daemonkit reports that kind of abort with the prior
  generation proved serving again, the install now runs once more from the
  top, inside the same three-minute budget. Any other failure, or a second
  such abort, still exits 1.

## [12.30.6] - 2026-09-14

### Fixed

- **Events from one agent run one at a time, and no worker is sent more than
  it can run.** The host's lane key read `session_id` with a strict decoder
  that refused every real Claude Code payload, so each hook invocation got its
  own lane and nothing was ever serialized; the lane count then widened the
  host's global admission to 64 while a worker runs 16 threads, so up to 48
  admitted events queued inside the interpreter where load shedding could not
  see them. Lanes are now keyed by `session_id` and `agent_id`, so a lead
  session and each of its subagents serialize their own events and overlap
  with each other, and the host holds at most `WorkerThreads` (16) events in
  flight per worker, shedding a dispatch whose wait there would outlast its
  deadline.

## [12.30.5] - 2026-09-14

### Fixed

- **Codex-backed nudges run again under a ChatGPT sign-in.** spawnllm 0.13.2
  resolves the codex `small` and `medium` tiers to `gpt-5.6-luna` instead of
  `gpt-5.4-mini`, which codex rejects with a 400 for ChatGPT accounts. Every
  `llm_nudge` routed to those tiers had failed on each call since the switch.

## [12.30.4] - 2026-09-14

### Fixed

- **Tracebacks stop rendering frame locals.** Logging a failed llm nudge
  rendered variables holding a 124 MiB transcript separately for each of the
  worker's three loguru handlers. In a replay with the nudge's signal gate
  forced, peak resident memory fell from 8,418 MiB on 12.30.3 to 1,477 MiB.
  All five loguru handlers now disable `diagnose`. Tracebacks keep the file,
  line, source and exception, and stop writing prompt and transcript content
  into logs through variable values. Transcript re-reading and `call_llm`'s
  transcript rendering still cause transient allocations.

## [12.30.3] - 2026-09-14

### Fixed

- **Deep conditions stop holding every subagent transcript.** A worker serving
  two busy lead sessions reached 4.4-4.8 GiB RSS as their subagent trees
  exceeded the cache budget and evicted each other's sessions. cc-transcript
  14.17.0 answers conditions from small per-file inputs; Captain Hook's own
  tree-walking conditions now use those inputs too. Errored calls stay hidden,
  and matching rules are unchanged. Direct checks over both live trees with
  the packs' 11 `RanCommand` argument lists took 52-287 ms per lead per warm
  event, down from about 1.76 s. With both main transcripts loaded and both
  trees answered, resident memory was 973 MiB, down from 3.35 GiB.

## [12.30.2] - 2026-09-14

### Fixed

- **A hook started from a deleted directory no longer hangs.** `capt-hookd run`
  resolved its working directory with libc's `getcwd`, which scans the former
  parent once the directory is gone. Claude Code sessions that tools such as
  slop-cop run in `$TMPDIR` delete their directory before `SessionEnd` fires,
  and against a `$TMPDIR` of 43,000 entries that other processes keep changing,
  those `SessionEnd` clients ran for over 30 minutes, past the 30 s request
  deadline, which starts only after the directory is known. The client now
  names its directory with `F_GETPATH` and falls back to `$PWD` when that
  directory is gone, as before.

## [12.30.1] - 2026-09-14

### Fixed

- **A slow session start returns what it has instead of timing out.** On a
  loaded machine, a cold `SessionStart` ran worker startup, discovery, the PR
  and fault announcements, and session-dir reaping in series, and passed the
  client's 30 s deadline. Claude Code then reported `context deadline exceeded`
  and dropped the whole reply, preload context included. A sync dispatch now
  stops starting hooks once 5 s or less remain before the caller's deadline
  and replies with what the hooks that ran produced. A skipped announcement
  stays pending and surfaces at the next session start. Reaping stale session
  dirs moves to the async `SessionStart`, off the path Claude Code waits on.

## [12.30.0] - 2026-09-14

### Changed

- **Hooks dispatch straight to the signed host, one Python exec fewer per
  event.** `bin/hook` now runs `capt-hookd` inside the installed Captain Hook
  app instead of the Python `capt_hook_client` shim, which saves about 34 ms on
  every hook event. An app older than 12.29.0 is refused with `brew upgrade
  yasyf/tap/captain-hook` rather than handed an argv it cannot parse.
  `SessionStart` keeps running through the Python shim, so the updater still
  runs on a machine whose app is too old to dispatch.

### Fixed

- **An upgrade no longer parks the old host over a worker that already left.**
  The host's shutdown gave a worker's surviving children a 5 s clock of their
  own after the worker exited, so a worker session that settled at 5.7 s
  under a 4.5 s budget was reported unproven and the old host parked holding
  its lock until launchd gave up. daemonkit 0.28.1 proves the whole session
  inside the shutdown stage's own deadline.

## [12.29.0] - 2026-09-14

### Added

- **`capt-hookd run` accepts Claude Code's hook argv directly.** It takes
  `[--root ROOT] run EVENT [--async]` and builds the same request the Python
  `capt_hook_client` shim builds: the root comes from `--root`, then
  `CLAUDE_PROJECT_DIR`, then `FACTORY_PROJECT_DIR`, then the working directory,
  and a deleted working directory falls back to `$PWD`, then `/`. The shim's
  `run --event` form still works. Nothing dispatches through the new form yet;
  a later release points hooks at the signed host and drops one Python exec,
  about 34 ms, from every hook event.

### Fixed

- **A new release installs even when uv's package index cache is stale.** The
  host installs its Python tool with `uv tool install --refresh-package
  capt-hook`. uv used to answer from a cached PyPI index written before the
  release, so hooks failed with `no version of capt-hook==<version>`, which is
  what took hooks down on 12.28.0.
- **An upgrade leaves hooks down for less time.** `package-install` signals an
  old host that will not leave once its 30 s shutdown grace plus a quarter has
  passed, at 37.5 s, where it used to wait out half its three-minute budget
  first. The host's request drain also waits for the handlers it cancels to
  return, so it no longer parks holding its lock while they finish.
- **Deep transcript conditions stop reparsing every sidechain on every event.**
  `RanCommand`, `UsedSkill`, `UsedTool` and `ReadFile` walk the whole sidechain
  tree by default. cc-transcript held lifted sidechains only up to
  64 MiB, admitted in first-come order and never evicted, so a worker serving a
  large lead reparsed everything outside that budget for each condition. One
  lead with 1,215 sidechain files and 872 MB cost about 16 s of CPU per
  `PostToolUse` dispatch. The cc-transcript pin is now exactly `14.16.1`, whose
  hold is a least-recently-used cache of 1 GiB of source bytes. A synthetic
  1,000-sidechain tree now costs 0.27 s per warm event, down from 2.6 s.
- **The host no longer collapses under a burst of hook events.** A 30-second
  caller timeout abandoned the request on the host side and released its lane
  and pool slot at once, so the host kept sending the Python worker more
  events while it was still running the ones their callers had given up on;
  the worker ran every one of them to completion on its 16 threads, and the
  request carried no deadline for it to check. Under a team of subagents this
  fed back into 70 to 140 clients queued behind one interpreter, every one
  timing out at 30 seconds. Three changes close the loop. The event request
  now carries `deadline_unix_ms`, which the worker checks when a thread picks
  the event up (an expired one is answered `deadline passed before dispatch`
  without running a hook) and again between hooks, so a dispatch stops once
  its caller is gone. An abandoned request keeps its lane and slot held until
  the worker actually answers it or dies, so what is in flight on the
  interpreter never exceeds the scheduler's ceiling. And a dispatch whose wait
  is already lost — the queue ahead of it at the last observed service time
  outlasts what is left of its deadline — is refused at once with `captain:
  host overloaded; hook skipped` instead of holding the tool call for the
  whole deadline; one that can still make its deadline queues, since a shed
  sync hook is a guard skipped. Known issue, left as it runs: the lane key
  decodes the payload strictly, so every real Claude Code event falls back to
  the client pid and no session has ever been serialized in production.
- **Hooks reuse repo language scans and transcript waiting checks.** Each
  event re-walked the repo, and each waiting check re-read the whole
  transcript. Hooks now cache those results and default each worker's
  transcript parser to four threads, honoring an existing thread setting.
  Root-level marker changes and root `.gitignore` edits take effect on the
  next event; nested changes take effect within 30 s. Transcript changes
  trigger a new waiting probe. In a 360-dispatch replay, median `PreToolUse`,
  `PostToolUse`, and async `PostToolUse` latency fell from 222, 212, and
  167 ms to 76, 37, and 14 ms, respectively.
- **A nudge whose model codex rejects fails once.** A codex ChatGPT-account
  sign-in rejecting a nudge's model caused three attempts per call, holding
  the session lane for about 9 s. Captain Hook now attempts once and
  remembers the rejection per specialty and model for the worker's lifetime.
- **The host stops its workers all at once.** `restart-workers` and the host's
  shutdown stopped the cached Python interpreters one after another on a
  single shared deadline, so each settlement spent what the next had left and
  the tail of a long list was demanded on a deadline already gone. daemonkit
  then published those workers unproven the instant SIGKILL went out, kept
  their records, and the next host shutdown faulted its children stage over
  processes that had died a millisecond later — `child N: process did not
  provably exit`, six at a time in the host log — and parked holding the
  flock until launchd's timeout. Every worker is now stopped concurrently on
  the whole budget.

## [12.28.1] - 2026-09-14

### Fixed

- **`brew upgrade` installs Captain Hook again under Homebrew 7.** Homebrew's
  macOS build sandbox now denies mach-lookup outside a short allowlist, and
  `xcrun stapler validate` needs LaunchServices, so the formula's `install`
  step failed with `kLSDataUnavailableErr` and every install or upgrade of the
  `captain-hook` formula aborted, including the self-updater's. The formula
  template no longer runs `stapler` inside the sandbox; `codesign --verify
  --deep --strict` still runs there, and the release pipeline's `smoke-draft`
  job validates the staple on a fresh runner before anything publishes.

## [12.28.0] - 2026-09-14

### Fixed

- **An upgrade no longer leaves hooks down when the old host will not leave.**
  `go.mod` upgrades daemonkit to v0.26.0, whose deploy runs inside the incoming
  `capt-hookd package-install`. An incumbent parked over abandoned shutdown
  stages (no socket, no children, flock held) made the install wait out its
  3-minute deadline and fail with `process did not provably exit`; it is now
  sent SIGTERM, then SIGKILL, and proven gone. A process still running from the
  installed bundle, such as the `CaptainHookWidget.appex` extension macOS
  relaunches, failed the install with `live processes remain on the
  deployment's executables` after the host was already stopped; it is now
  terminated during quiesce. The candidate app is validated before anything
  stops, and any abort before the swap commits restarts the incumbent's
  services and proves its host ready again.

## [12.27.1] - 2026-09-14

### Added

- **Conformance tests check the Go and Python worker protocol together.**
  `tests/test_protocol_conformance.py` and
  `internal/wireproto/conformance_test.go` compare protocol constants, JSON
  fields, accepted and rejected frames, and round trips between languages.
  The Go wire contract moves from `internal/hookd` to `internal/wireproto`,
  which imports only the standard library so the Linux CI job can run it.
  The tests caught Python's stale 64 MiB frame limit;
  `captain_hook/worker/protocol.py` now derives `MAX_FRAME` as
  `MAX_HOST_PAYLOAD + 4 * 1024`, matching Go's 68,161,536-byte ceiling.

### Changed

- **The internal Python notification package is now `captain_hook.desktop`.**
  `captain_hook.helper` moves to `captain_hook/desktop/`, with imports updated
  in `captain_hook/cli.py`, the review modules, and
  `captain_hook/update/updater.py`. Its tests move to
  `tests/test_desktop_cli.py` and `tests/test_desktop_client.py`. The
  `capt-hook helper` command and its behavior are unchanged.

- **The repository layout docs cover every component, with `make` as the
  local build and test entrypoint.** The development guide fragment at
  `.claude/fragments/AGENTS.md/captain-hook-development-guide.fragment.md`
  now lists the client, Go daemon, `internal/wireproto`, Swift helper,
  stress and benchmark harnesses, and tutorial sources. The `structure-check`
  job in `.github/workflows/ci.yml` rejects undocumented top-level directories.
  The root `Makefile` replaces separate build and test invocations with
  `make`, covering Python, Go, Swift, and the tutorial's TypeScript bundles.
  `make python-test`, `make go-test`, and `make helper-test` run individual
  suites; a failed build or test returns a nonzero exit status.

### Fixed

- **A timed-out hook's late reply no longer kills the shared worker.** In
  12.27.0, `workerClient.call` in `internal/hookd/worker.go` dropped a
  timed-out request's id, so when the Python worker later answered it,
  `readLoop` read an unknown request id, failed the worker, and failed every
  other session's in-flight hooks with `captain: Python worker returned
  unknown request id N`. A timed-out id now moves to an abandoned set whose
  late reply is discarded; a reply for an id the host never issued still
  fails the worker as a protocol violation.

- **A hook's timeout no longer cancels worker startup for other sessions.**
  `workerManager.startEntry` in `internal/hookd/manager.go` starts Python
  workers on the daemon's lifetime, bounded by `workerReadinessTimeout`.
  Previously, a hook with a short timeout arriving first at a fresh root
  tore down the startup that other sessions' hooks were waiting on, failing
  them with `captain: handshake Python product worker captain: read worker
  frame header: read |0: file already closed`. Every caller of
  `workerManager.acquire`, including the first, now waits under its own
  timeout. If all callers leave, startup still finishes and caches the worker
  idle, or retires it immediately for a temporary root. A worker whose child
  dies before publication is never cached; the next request can start a new
  one. `internal/hookd/worker_start_test.go` covers these startup races.

## [12.27.0] - 2026-09-14

### Added

- **Dispatch latency and `execve` counts have a repeatable benchmark.**
  `uv run python -m bench.cli run` measures cold and warm dispatch through the
  client and the Go host, with `--record` writing `bench/baseline.json`.
  `bench/counter.py` reads the macOS system-wide exec counter without root;
  `bench/measure.py` pairs command and idle windows and reports `execs: null`
  when background activity prevents a confirmed count. `tests/test_bench.py`
  checks that the measurements detect both an extra exec and added latency.

### Changed

- **Go CI runs the test suite with the race detector.** The `go-test` job in
  `.github/workflows/ci.yml` runs `go test -race -count=1 ./...` on macOS.
  Previously, CI compiled `capt-hookd` in `helper-test` without running its
  Go tests; test failures and data races now fail CI, and `-count=1` prevents
  a cached pass from standing in for a new run.

- **The host admits 256 concurrent wire sessions, up from 64.**
  `hostConcurrency` in `internal/hookd/daemon.go` caps wire sessions, which
  resident clients hold for their lifetime. The higher ceiling gives
  those clients and in-flight hooks more session slots; dispatch concurrency
  remains governed by the scheduler.

### Fixed

- **One session's timeout or hook error no longer retires the shared worker.**
  On 12.26.0, `workerManager.dispatch` in `internal/hookd/manager.go` retired
  a cached Python worker on any call error, failing unrelated sessions with
  `use of closed network connection`. It now checks `workerClient.broken()`
  in `internal/hookd/worker.go` before retiring the interpreter. Caller
  deadlines and Python error frames leave the worker usable, timed-out calls
  remove their pending entries, and an oversized frame refused before a
  write leaves other callers alone. Transport failures still retire the
  worker.

- **Idle wire sessions release their slots after 15 minutes.** `go.mod`
  upgrades daemonkit to v0.25.0 and adopts the `Daemon.Idle` default. The idle
  timer runs only between requests, so in-flight hooks keep their slots.
  Suspended or disconnected peers previously held slots until a daemon restart,
  eventually preventing new hooks from attaching. An attach that meets a
  full session table now retries with a short backoff within its bounded
  attempt budget.

- **Session-capacity exhaustion no longer tells operators to reinstall a
  healthy host.** `probeFailure` in `internal/hookd/client.go` handles
  `daemonkit.ErrSessionCapacity` explicitly. The error now says the signed
  host is `serving its session ceiling` and `installed and healthy`, with
  hooks retrying on the next event. Previously, `wire: session capacity
  exhausted` was reported as "signed host is not installed and ready" and
  prescribed `capt-hook helper install`, which did not free a session slot.

## [12.24.0] - 2026-09-02

### Added

- **`CAPT_HOOK_MAX_PARALLEL` overrides the dispatch ceiling.** The demand-scaled
  budget below is clamped to a ceiling of `maxLiveWorkers` (64); this variable
  replaces that ceiling. An unparseable or non-positive value is fatal at daemon
  startup — `parallelCeiling` returns `(int, error)`, `newWorkerManager` returns
  it, and `server.go` surfaces it — so a typo fails loudly instead of silently
  halving throughput. Unset or blank is still the default.

### Changed

- **The dispatch budget scales with live sessions instead of sitting at 16.**
  `internal/hookd/scheduler.go` admitted dispatches through a fixed
  `chan struct{}` of 16. A lane gate already caps one session at one executing
  dispatch, so any global limit below the live session count made unrelated
  sessions queue behind each other for nothing: on a busy machine a warm hook
  invocation measured 8–17s wall for about 0.06s of CPU, blocking rather than
  computing. The semaphore is now a pool with an explicit FIFO waiter queue
  whose limit is re-evaluated on every admission, so the width changes while
  callers are queued. The limit tracks the live sync lane count clamped to
  [16, `maxLiveWorkers` (64)]: 16 is the old value, so a quiet machine behaves
  as before; 64 is the worker cache cap, past which no width reaches another
  interpreter. The same invocation on the same machine now takes 1.16–1.32s.
  Daemon-side scheduling only; the wire protocol is untouched.

- **Background dispatches have their own pool and their own lane.** They shared
  the 16 slots and the per-session lane with blocking dispatches, so background
  work took slots the blocking lane needed, and a session's blocking hook could
  serialize behind its own background one. `asyncParallelDispatch = 4` gives
  them a separate budget, and the async flag joins the lane key. `Async` was
  already on the wire.

### Fixed

- **A machine out of fork capacity no longer respawns `claude plugin list` on
  every dispatch.** `enabled_plugins` in `captain_hook/packs/plugins.py` cached
  nothing on a roster failure: the spawn failed with EAGAIN, nothing was
  written, and the next dispatch spawned it again — a self-feeding loop, with
  813 `PluginListError` and 167 `BlockingIOError: [Errno 35]` in one daemon
  log. The snapshot is also invalidated by machine-wide files, so one write to
  `installed_plugins.json` invalidated every cached root snapshot at once and
  every session spawned the CLI together. Four pieces. A `RosterFailure`
  negative cache re-raises for `FAILURE_TTL` (15s), bounded both by the
  watched-file stat tuple, so a fixed roster is picked up at once, and by the
  timer; an empty roster is still cached as a `PluginSnapshot`, never as a
  failure. `roster_gate()` admits at most `GATE_WIDTH` (4) concurrent spawns
  through per-slot `flock`s nested inside the existing per-root lock, so a
  machine-wide invalidation staggers rather than stampedes; the kernel releases
  an `flock` when its holder dies, so a crashed session cannot wedge a slot,
  and past `GATE_TIMEOUT_SECONDS` (10) a waiter overtakes the gate and spawns
  ungated rather than raising. `RosterUnavailable` is raised only where the
  CLI could not run at all (`TimeoutExpired`, `OSError`, EAGAIN) and records a
  machine-wide `RosterOutage` that queued waiters see on release, so a
  fork-exhausted machine costs one spawn per TTL instead of one per root;
  every other `PluginListError` (nonzero exit, bad JSON, corrupt shape) stays
  per-root. Enablement is never shared across roots: a single machine-wide
  roster snapshot was tried and rejected on evidence, because the `enabled`
  flag varies by cwd and drifted 120 → 124 between two back-to-back CLI runs
  from the same root with `installed_plugins.json` unchanged — concurrent
  sessions rewrite their own `settings.local.json`. One blast radius grew: a
  machine-wide outage suppresses plugin packs for every root for up to 15s,
  where `cli.py` already degrades to `[]` with the fault recorded rather than
  failing the session.

- **A session whose workspace was reaped before it first needed a worker gets
  one.** Fourth and last fix for the deleted-workspace condition 12.23.2
  through 12.23.4 chased. `workerCmd` in `internal/hookd/manager.go` spawned
  the Python worker with `Dir: key.root`; when that root no longer exists the
  chdir fails, and Go reports the failure as `ENOENT` naming the executable,
  so the operator saw `posix_spawn .../bin/python: no such file or directory`
  for an interpreter that exists and runs. It stayed hidden through the
  earlier fixes because workers are pooled per root: one spawned while the
  root existed kept serving after the deletion, which is why the outage
  presented as a crash deep in `worker_log_key` rather than a spawn failure.
  `workerDir` returns the root when it is an existing directory and
  `os.TempDir()` otherwise. An empty directory detects the same languages a
  deleted one would, and `-P` still keeps the Dir off `sys.path`.
  `capt-hookd serve` still treats a live pid in `daemon.records` as proof the
  daemon is serving, with no check that the socket exists.

## [12.23.4] - 2026-09-02

### Fixed

- **A hook fired from a deleted workspace no longer walks the whole
  filesystem.** 12.23.3 made `_cwd()` return `/` when `os.getcwd()` raised, and
  with neither `CLAUDE_PROJECT_DIR` nor `FACTORY_PROJECT_DIR` set `--root` is
  spelled from the same value, so `packs/manager.detect_languages` walked the
  machine from `/`. On macOS that walk reaches the synthetic `/.resolve`, which
  stats with `EINVAL` rather than a missing-file error, and `Path.is_file`
  propagated it: `OSError: [Errno 22] Invalid argument: '/.resolve/.gitignore'`.
  `_cwd()` now falls back to `PWD` before the filesystem root — the shell still
  carries the path after the directory is unlinked, so a root that does not
  exist walks to nothing — and `/` remains the last resort when `PWD` is unset
  too. A real Claude Code session always sets `CLAUDE_PROJECT_DIR`; a probe
  that deliberately unset it found the walk, and no user hit it.

- **`gitignore_lines` returns `[]` on any `OSError`, not only an absent file.**
  Any walk root can contain a path that stats with something other than
  `ENOENT`, and one such path should not end a dispatch.

## [12.23.3] - 2026-09-02

### Fixed

- **A hook fired from a deleted workspace is spelled instead of failing before
  dispatch.** Verifying 12.23.2 on the machine with a real dispatch from a
  deleted directory surfaced the same condition one layer up, in the client
  shim. `capt_hook_client/client.py` read `os.getcwd()` to spell `--cwd`, and
  `--root` too when neither `CLAUDE_PROJECT_DIR` nor `FACTORY_PROJECT_DIR` is
  set, so every later hook from a session whose workspace Orca had deleted died
  with `FileNotFoundError` at `client.py:27`. The new `_cwd()` returns
  `os.getcwd()`, or the filesystem root once that directory is gone. The blast
  radius is why this is its own release rather than part of 12.23.2: the worker
  crash took the host's socket down for every session on the machine, where
  this failed only the one invocation that had no cwd — with 12.23.2 deployed,
  `daemon.sock` survived such a dispatch. `--root` precedence is unchanged: an
  explicit `--root`, then `CLAUDE_PROJECT_DIR`, then `FACTORY_PROJECT_DIR`,
  then the cwd.

## [12.23.2] - 2026-09-02

### Fixed

- **A session whose workspace was deleted under it no longer takes the host
  down.** Every prompt on a machine was failing its Stop hook with
  `daemon unavailable`: `capt-hookd` was alive but its socket was gone,
  `capt-hookd serve` refused to start a replacement because the pids in
  `daemon.records` were running, and launchd did not own the process, so only
  killing the process group and kickstarting restored it. The daemon log held
  one traceback on repeat, from dispatches under an `~/.orca/workspaces/…` root
  that no longer existed. `worker_log_key` in `captain_hook/worker/__main__.py`
  resolved the root through `os.path.realpath(os.getcwd())`, which raises
  `FileNotFoundError` once the process's cwd has been deleted, and the call
  sits in `main()` before `WorkerService` starts — so every dispatch from such
  a session killed its worker at startup, and the crash-looping workers took
  the socket with them. The per-root key stays (loguru rotation is
  per-process, and same-build workers sharing one file strand each other's
  handles on the unlinked inode); when the cwd is unresolvable the key derives
  from the pid instead. Orca reaps workspace directories under sessions that
  are still alive and firing hooks, so the worker tolerates the condition
  rather than crashing.

## [12.23.1] - 2026-09-02

### Changed

- **`authoring-hooks` routes bash-command guards to `Runs`, not a regex.** The
  primitive table sent every bash guard to `block_command`, whose first
  positional argument is a pattern, and pitfall 5 presupposed a regex for
  command rules while offering file rules a structural alternative; `Runs`
  appeared once, in a reference table off the decision path. The table now
  names `hook(..., only_if=[Tool("Bash"), Runs(...)], block=True)`, with
  `block_command` reserved for text no argv prefix names (a flag's value, a
  substring), and the pitfall explains that `Runs("git", "stash")` compares
  exact tokens against the argv prefix of every parsed command, so
  `echo git stash` and a heredoc body never fire.

### Fixed

- **The `general` pack's prose guards fire on an unpinned spawn or stage, not
  only a wrong pin.** Both were built on a premise the routing doctrine no
  longer holds: that a delegated lane naming no model inherits the session
  model, fable. A subagent or workflow stage with no `model` runs opus whatever
  the root runs, so a missing pin misroutes prose exactly as an opus pin does,
  and `prose_spawn_gate` (requiring `ToolInput("model", haiku|sonnet|opus)`)
  and `prose_workflow_nudge` (requiring `WorkflowScript(model=…)`) let it
  through silently. Worse, the gate's own context told the judge the opposite:
  the unpinned line read `(none — inherits the session model, fable)`, and the
  shared workflow header fragment said an unquoted stage was already correctly
  routed. The gate now fires on every `Agent`/`Task` spawn behind the same
  `ProseSpawn` prefilter, with `skip_if` on `ToolInput("model", fable)`; the
  nudge drops the model condition and lets the judge decide per stage, with
  deliberately no per-script fable skip, since one pinned stage would mask an
  unpinned prose stage elsewhere in the script. `ProseSpawn` overrides a new
  `DelegatedSpawn.unpinned_note` so the context reads
  `(none — an unpinned subagent runs opus; subagents never inherit fable)`; the
  default is unchanged for the other spawn hooks.
  `fragments/workflow_script_header.md`, the four "inherits fable"
  parentheticals in `review_routing_workflow_nudge.md`, and the
  `WorkflowScriptSource` header lead in `contexts.py` say the same.
  `implementation_spawn_nudge` and `review_routing_spawn_nudge` still describe
  an unpinned spawn as inheriting fable through the unchanged default.

## [12.23.0] - 2026-09-02

### Changed

- cc-transcript pin raised to exactly `14.16.0`: relay envelopes (`Another Claude session sent a
  message:`, `<agent-message>`, `<cross-session-message>`) are now `is_agent_injected`, so a root
  session's recent prompts are the human's asks, never relays.

### Fixed

- **A hook firing inside a subagent or teammate lane now reads the lane's own
  transcript, not its orchestrator's.** Claude Code attaches
  `agent_transcript_path` only to `SubagentStop`; every tool event fired inside a
  lane carries the lane's `agent_id` and the *parent* session's
  `transcript_path`, so `dispatch_event` built `evt.ctx.transcript` from the
  parent and every LLM judge read the orchestrator's prompts as the lane's task.
  On a tool event carrying `agent_id` but no `agent_transcript_path`, the lane
  file now resolves by convention through the new
  `transcripts.lane_transcript_path` — `<parent-dir>/<parent-stem>/subagents/agent-<agent_id>.jsonl`.
  An `agent_transcript_path` on the payload still wins. Every other event keeps
  reading `transcript_path` as given: `SubagentStart` fires in the *parent's*
  turn while carrying the spawned lane's `agent_id`, and steering hooks that read
  the in-flight `Agent` call need the parent transcript, not a lane file Claude
  Code has yet to write.

- **Lane transcripts segment into turns instead of yielding zero prompts.** A
  lane's brief and the messages its team sends it are sidechain user events,
  which the native classifier rejects — a real 122-event lane file lifted to one
  turn and no prompts, so `UserMessages()` rendered nothing and `required`
  contexts skipped the judge outright. The new `lane` classifier keeps sidechain
  and teammate-relayed prompts (dropping only meta, compact-summary,
  interrupted, and text-less events) and is detected first in the chain — from a
  `subagents/` transcript path, or from an event stream whose user events are all
  sidechain — since a lane under droid or conductor still needs lane semantics.

- **The `detours` judge no longer reads a lane's brief as an orchestrator
  conversation.** Its `<user_messages>` evidence bullet now states that for a
  delegated agent the block is the lane's own brief plus later messages from the
  team, that the orchestrator's conversation is not in evidence, and that a peer
  teammate's message is information, not authorization.

## [12.22.5] - 2026-08-30

### Fixed

- **`brew upgrade captain-hook` stops failing on every host, and the deployment
  moves out of Homebrew's sandbox.** The formula's `post_install` shelled
  `capt-hookd package-install`, whose last step writes
  `~/Library/LaunchAgents/<label>.plist` — a write Homebrew's post-install
  sandbox denies. Every `brew upgrade`/`reinstall` therefore exited non-zero,
  on both fleet machines, every time. What made it survivable was also what
  hid it: the denial lands *after* the app bundle is superseded, so the
  deployment converged anyway and only the exit code was wrong. `post_install`
  is gone. A new `deploy(target)` runs `brew --prefix` → that Cellar's
  `capt-hookd package-install` → a host ping, outside any sandbox, and both
  `capt-hook helper install` and the self-updater's `run_update` go through it.
  **A bare `brew install captain-hook` no longer deploys the application** — it
  fills the Cellar, and `capt-hook helper install` (which the formula's
  `caveats` already names) lands and activates it. Because `package-install`
  supersedes the running daemon, `MAX_ESCALATIONS` now gates the whole apply
  lane rather than only the reinstall/install rungs: a host that cannot
  converge stops trying after its budget instead of taking hook dispatch
  down once per window forever.

- **`capt-hook helper install` no longer reports success it did not verify.**
  It read `brew install`'s exit status and printed "Captain Hook installed"
  unconditionally — the last instance of the brew-exit-code bug 12.22.4 fixed
  in the updater. It now compares the Cellar version against the version the
  running host reports over a ping, and fails naming both when they disagree.

- **A hook module that fails to import is now announced.** `loader.py` logged
  the `LoadError` and appended it to the load-error list but never called
  `faults.record`, unlike the plugin-roster path. Nothing surfaced at session
  start, so a broken `PreToolUse` guard silently let every tool call through.

- **A repo-local hook can import a sibling module at the project root.**
  `CliState.discover` now appends the resolved project root to `sys.path`.
  Appending, never inserting: the root stays below site-packages, so this
  cannot reopen the import shadowing the `-P` flag closed.

- **The worker installs the daemon log sinks again.** `configure_daemon_logging`
  lost its only production caller when `d7d554d` (2026-07-21) deleted the Python
  daemon lifecycle; the replacement entry point picked up that commit's other
  two responsibilities but not this one. Per-session log files have been empty
  since — 69 of the 81 files in `~/.cache/captain-hook/logs` are zero bytes, and
  the newest with content is dated the day after that commit. The sink is keyed
  on the build plus a digest of the worker's project root, matching the per-root
  key the deleted helper used: the host runs one worker per root, and loguru
  rotates per process, so a shared file would let one worker's rename strand the
  others' handles on the unlinked inode.

- **Three release gates that could never fail now can.** In `release-pypi.yml`,
  `! grep -Fq …` does not trip `errexit`, so the formula's cask, absolute-path,
  and `package-install` guards passed against any template. Each is now an
  explicit `if grep …; then exit 1; fi`.

- **A namespace hooks package no longer leaks between tests.**
  `conftest.isolate_modules` evicted only modules with a truthy `__file__`,
  which a namespace package never has, so a `hooks` package survived with its
  `__path__` pinned to a deleted temp directory.

## [12.22.4] - 2026-08-29

### Fixed

- The Code Stewardship nudge (`steering.steering:nudge_1ebed8c4`) no longer fires on
  consequence-describing prose. Its prospective-tense `leave` clause matched any sentence
  naming the noun class (bug, issue, failure, ...) as the grammatical subject, including
  a bug's own consequences ("a failure would leave a partition looking more recently used
  than it was") rather than an agent choosing to leave something unfixed. The clause now
  requires a pronoun or imperative subject, and the regression pair from the misfire
  (session `30c00a95`) is checked in alongside the genuine pronoun-subject case.

## [12.22.3] - 2026-08-28

### Fixed

- NLP signal matching no longer reads the WordNet lexicon from several dispatch threads at once.
  `ensure_wn_lexicon` sets `wn.config.allow_multithreading`, which drops Python's same-thread
  assertion but leaves wn pooling a single process-global sqlite connection; serialized sqlite
  protects the C handle, not that connection's prepared-statement cache, so two threads running
  one query reset a statement under the other's step. Sixteen threads reproduced 2521 failures in
  6400 calls — 1985 `sqlite3.InterfaceError: bad parameter or other API misuse`, and 534
  `IndexError` short rows, which is the worse half: a row read from a statement already reset
  looks like a real answer, so a clause could match on lemmas the lexicon never returned. One
  machine's daemon log carried 62 of the visible failures. Lemma lookups now run one thread at a
  time and resolve to strings inside the lock, since a `Synset` queries again on attribute access.
  Lemmas stay cached per phrase, so steady-state dispatch never reaches the lock.


## [12.22.2] - 2026-08-28

### Fixed

- A worker no longer loses its readiness handshake to the login shell it asks for the user's
  `PATH`. `adopt_user_path()` probed `printenv PATH` through the passwd login shell on every
  worker spawn, ahead of the readiness frame and inside hookd's 10s `workerReadinessTimeout`, and
  a login shell is the most expensive thing that happens in that window: `fish -l` measured
  1495 ms per run on an idle machine, against 450 ms for the worker's entire import graph. Under a
  parallel agent fleet it went past the 5s probe cap outright, hookd tore the child down, and the
  hook reported `captain: handshake Python product worker` over a pipe `Wait()` had already
  closed. A worker that survived the timeout was worse: the probe failure is non-fatal, so `PATH`
  stayed launchd-minimal, `which("claude")` returned `None`, and every plugin-shipped pack went
  quietly unloaded behind one `PluginListError` — 2130 of them in a single machine's log. The
  answer is now cached, keyed on the login shell so changing shells is a miss rather than a stale
  hit, and a refresh sitting behind a usable record runs on a 1s budget and falls back to that
  record rather than surrendering the worker's `PATH`. 1500 ms cold, 0.3 ms warm.

- The binrun descriptors' version probe execs the signed helper by absolute path instead of
  resolving `capt-hook-host` through `PATH`. Homebrew unlinks the active keg before installing its
  replacement, so any upgrade of the formula opened a window in which the probe exited 127 and live
  sessions failed with `binrun: run version command: exit status 127`.

## [12.22.0] - 2026-08-26

### Changed

- Requires daemonkit v0.23.0 on both halves, and `HelperPaths` no longer derives the host socket
  itself. The Swift side hand-reimplemented daemonkit's label-to-socket formula because the Swift
  package had no such helper, and the SPM pin sat a release behind the Go one to keep that copy
  agreeing; daemonkit v0.23.0 adds `AgentPaths`, so both halves now derive the path from the same
  place and the copy is gone. The host's state moves from `~/com.yasyf.captain-hook.host.v1` to
  `~/.daemonkit/a/com.yasyf.captain-hook.host.v1`. There is no migration and no fallback read: the
  host comes up fresh under the new root, and the old directory stays where it lies until deleted
  by hand once the host is confirmed serving under the new one.

- cc-transcript pin raised to exactly `14.15.0`, which stops a deep predicate reparsing the whole
  sidechain tree on every call. `has_command` and its siblings walk every reachable transcript when
  `subagents=True`, and each walk used to parse and lift every node from disk again — ~100 walks and
  ~480 transcript parses per dispatch on a session with 60 sidechains. Together with the gitignore
  fix below, one `PostToolUse` dispatch against a large monorepo and a 27 MB transcript drops from
  1678 ms to 198 ms, measured against this pin.

- Agent-launched sessions now take part in the update check. `dispatch_update` skipped every
  headless session outright, so on a machine where most sessions are agent-launched the check fell
  to whichever interactive session happened along — 1,403 of 1,516 lines in one update log were
  `update skip: sdk entrypoint`. A headless session now runs the same release check and records
  what it finds, but never runs a brew lane: converging supersedes the running daemon, and an agent
  session acting on that would drain hook dispatch under every other session the daemon serves. The
  next interactive session applies the deferral immediately, claiming an apply stamp of its own
  rather than waiting out a check window its headless peer already claimed, and only one such apply
  runs per window.

### Fixed

- The self-updater reported success on every window while never converging. `run_update` read
  `brew upgrade`'s exit status as proof of an upgrade, but `brew upgrade` exits 0 against a Cellar
  that already carries the release, and only `reinstall`/`install` re-run the formula's
  `post_install` — the step that supersedes the deployed app under `~/Applications`. A machine
  whose Cellar upgraded without landing the app therefore logged `update ok: 12.21.4 -> v12.21.6`
  every window while the host it actually ran stayed 12.21.4; one went six days and two releases
  that way, and would have swallowed every release after them. Each brew lane's outcome is now read
  back from the host's own ping, `update ok` is written only once the host has reached the release,
  and the reinstall escalation is bounded to three attempts per release tag so a deployment that
  cannot converge stops reinstalling forever.
- Dispatch no longer recompiles a repository's gitignore patterns once per directory
  it walks. Every event fingerprints the project to decide whether the cached hook
  registry still holds, and that fingerprint walks the tree for build manifests with
  real gitignore semantics: patterns accumulate down the tree, and the accumulation
  was compiled into a fresh `GitIgnoreSpec` at every directory. On a large monorepo
  that came to 85,000 pattern compilations and ~390 ms of CPU per hook event, paid
  again by every hook process the event fans out to. A directory carrying no
  `.gitignore` now inherits its parent's compiled spec, and one carrying its own
  compiles only its own lines and concatenates the patterns — the same order, the
  same matches. The walk drops to ~47 ms on the same repository, and a differential
  run over 216 trees, three real repositories and 200 generated gitignore layouts
  among them, agrees with the old walk on every one.

## [12.21.6] - 2026-08-19

### Fixed
- **The `graphite` pack fired in repositories that had opted out of the gt lane.** Every
  hook gated on `GraphiteActive`, which tested only for a `.git/.graphite_repo_config`
  marker — so a repository carrying a stale marker while setting `ccx.nogt` (ccx's own
  durable opt-out, which makes `ccx vcs ship` decline the gt lane) was still told its
  workflow was Graphite. The jj ban is the sharp end: it is `block=True`, so a colocated
  jj repo that had opted out could not run `jj` at all. `GraphiteActive` now resolves the
  lane, not just the marker, reading `ccx.nogt` through the git common dir so a linked
  worktree inherits its main checkout's answer, and memoizing it — the probe runs only in
  a repository that already carries the marker, so nothing else pays for it. A `git` that
  cannot answer leaves the guards armed, matching ccx, which treats an unreadable key as
  "not set". Note the accepted spellings are Go's `strconv.ParseBool` set, not
  `git config --type=bool`'s: `yes`/`on` do **not** disable the lane, in either tool.
- **The jj ban blocked reads.** `jj log`, `jj status`, `jj diff`, `jj bookmark list`, even
  `jj --help` were denied, though the ban exists to protect stack metadata and a read
  mutates none. Read-only `jj` is now carved out — but only when *every* `jj` call on the
  line is a read, so `jj log && jj new` is still blocked, and a verb outside the read set
  (a newly added one included) still blocks rather than leaking.

## [12.21.4] - 2026-08-17

### Fixed

- The daemon no longer holds a Python interpreter for every project root it has
  ever seen. Workers are cached per `{root, python, build, environment}` tuple,
  and were dropped only when one died, failed a call, or the daemon restarted.
  Roots that never repeat then accumulated. Per-invocation scratch directories
  and short-lived worktrees filled all 64 slots with interpreters no dispatch
  would ask for again. One machine was carrying 12.3 GB of them, idle, one key
  away from refusing every new project. Four changes now bound the cache. An
  idle worker retires after 30 minutes. A full cache evicts its least recently
  used idle worker instead of refusing admission. A scratch root under `TMPDIR`
  retires as soon as its last dispatch finishes, without caching at all. And a
  worker whose root has been deleted is swept.

## [12.21.3] - 2026-08-16

### Fixed

- Releases publish again. `uv` now emits core packaging metadata 2.5, which the
  pinned `gh-action-pypi-publish` rejected outright — so `publish-pypi` failed
  and took `publish-github`, `sync-plugin-version`, and `helper-formula` down
  with it, leaving `12.21.1` and `12.21.2` as draft releases with no published
  Homebrew formula. The action moves to v1.14.2, whose Twine 7 accepts that
  metadata version.

## [12.21.2] - 2026-08-16

### Fixed

- The worker no longer searches the session's own repository for the commands it
  shells out to. `12.21.1` adopted the user's login `PATH` verbatim, and POSIX
  resolves an empty entry against the current directory — which for a worker is
  the repository under review. A user whose `PATH` carried an empty, `.`, or
  relative entry would have let an executable named `claude`, `codex`, or `gh`,
  committed to any repository they opened, run automatically as them. Only
  absolute entries are kept now, from the inherited `PATH` as well as the login
  one.
- A transient `claude plugin list` failure no longer disables plugin-shipped
  guards until something else changes. `12.21.1` stopped caching the failure in
  the roster snapshot, but the daemon's registry still cached the builtins-only
  discovery it produced, so one bad enumeration held every plugin guard off
  until a watched file moved or the worker restarted. A discovery whose roster
  failed is now served and dropped rather than kept.
- The login-shell `PATH` probe no longer reads the worker's stdin, where an
  unguarded `read` in a startup file would consume the daemon's length-prefixed
  hello frame and cost the worker its readiness handshake.
- The probe finds its answer by tag rather than by position. It previously took
  the last non-empty line, which `.zlogout` output would displace — installing
  something like `session closed` as the `PATH` and making every user command
  invisible.
- A recorded fault is announced only to the project it came from. Fault text is
  a raw exception, the store is machine-wide, and a session start injects it
  into that session's context, so one project's paths could surface in another's.
  Faults from captain-hook's own machinery stay machine-wide.
- `first seen` is now the first occurrence. A repeat used to overwrite the
  record's timestamp while still being labelled as the first.
- At the record cap the oldest fault makes way instead of the newest being
  dropped, so a hook whose error text carries a changing path can no longer fill
  the store and mask everything after it.

## [12.21.1] - 2026-08-16

### Added

- Failures that land where no hook response can carry them now reach the user.
  Async review and update dispatch run on a worker thread after the response
  Claude Code reads has gone back, and a hook handler's exception is caught so
  one bad hook cannot take the event down. Both used to land in the daemon log
  and nowhere else, which is how a live `ModuleNotFoundError` on every
  `SessionStart` survived a green bill of health. Those failures are now
  recorded as durable machine-wide faults, drained and announced at the next
  session start. Repeats collapse into one line, and the record caps at 32 so
  an error text carrying a path cannot flood it.

### Fixed

- Plugin-shipped guards fire again on a machine that installs one plugin in
  more than one project. `claude plugin list` answers machine-wide whatever
  directory it runs in, and grouping its roster by plugin id alone read one
  plugin installed at the same scope in several projects as that many competing
  installs of one id. The caller swallowed that error into an empty roster, so
  every plugin-shipped hook on the machine silently stopped firing while
  everything reported healthy. The roster is now scoped to the discovering
  project before deduping, an unreadable roster raises instead of caching empty
  — surfaced in `capt-hook status`, `capt-hook pack list`, and at the next
  session start — and a snapshot version gate recomputes caches written by
  either broken path.
- A session driven from a repo named after one of capt-hook's own dependencies
  no longer crashes every hook routed through the worker. Spawning
  `-m captain_hook…` with the session's repo as cwd put that repo at the head
  of `sys.path`, so a directory sharing an installed dependency's import name
  shadowed it, and the import died inside the worker thread, after the
  response Claude Code reads had already gone back, with a traceback pointing
  at the user's source tree. Every spawn site now passes `-P`, keeping cwd off
  `sys.path`.
- The detached reviewer finds its judge backend again. launchd starts the
  helper with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so `claude`, `codex`, and
  `gh` were invisible to the daemon and everything it spawned: the reviewer
  raised `BackendUnavailable` while the identical probe from a terminal
  succeeded. The worker now asks the user's login shell for its exported `PATH`
  once at startup, resolving the shell through the passwd database because
  launchd sets no `SHELL`.
- `capt-hook mcp` starts. The server has been dead in every shipped build since
  mcp 2.0.0's release: `pyproject.toml` pinned `mcp>=1.9` unbounded while
  `uv.lock` froze 1.28.1, so CI tested 1.x while every release-shaped install
  resolved 2.0.0, which removed `mcp.server.fastmcp` outright. The server now
  runs on the 2.x `MCPServer` with the pin bounded to `mcp>=2.0,<3`; the one
  tool, `register_transcript`, keeps its exact name and schemas, and
  `serverInfo.version` reports capt-hook's own version instead of the MCP
  SDK's. CI's wheel smoke now drives a real stdio handshake, because a server
  listed in the plugin manifest is not a server that starts.

## [12.21.0] - 2026-08-03

### Changed

- Captain Hook runs on daemonkit v0.21.2. The host declares one `Daemon` value
  both halves share, serves over a socket derived from its launchd label
  rather than a path under `Library/Caches`, and the four peer roles collapse
  into daemonkit's two session lanes. The broker sidecar is gone: broker
  handoff is control-lane-only in v0.21, so the bridge CLI dials the host
  socket directly on the business lane, verifying the host's stamped build
  over that same session before it forwards anything.
- Workers are supervised through daemonkit's process ownership. Each Python
  worker runs in its own session group so hook subprocesses die with it, and
  the worker environment seeds the host's `PATH` and `LANG=C` exactly as it
  did before the migration.

### Fixed

- Installing over a running helper works again. Both the upgrade and the
  uninstall inventory every executable in the installed bundle and refuse while
  any of them is alive, but nothing stopped the helper app first — so a machine
  with Captain Hook running met a live-process refusal instead of an upgrade.
  The packaged app's signed stop entrypoint now terminates the
  installed generation before either verb touches the bundle. Finding nothing to
  stop counts as success, so a first install on a clean machine is unaffected.
- Upgrading from a release older than this one no longer fails on its own
  running host. That host predates the current ownership records, which makes it
  invisible to the check that would otherwise drain it and fatal to the
  executable inventory that runs next, so the upgrade refused every machine it
  was meant to migrate. An installation with no such record is now stopped
  before the upgrade proceeds. One that has a record is left alone, since the
  ordinary drain already covers it.
- Uninstalling a helper installed by an earlier release no longer leaves its
  LaunchAgents loaded. Those installations wrote plists carrying no ownership
  marker, and the current deployment machinery removes only labels it has a
  record of applying — so an uninstall could delete the application while
  launchd kept both jobs loaded against the path it had just removed, trying to
  relaunch a binary that was gone. Uninstall now drains whatever serves the host
  label and takes both agents down, whichever era wrote them.
- A machine can no longer accumulate unbounded Python workers. The host caches
  one worker per distinct project, interpreter, build, and hook environment, and
  the 64-worker ceiling enforced before the process manager was replaced went
  with it. That ceiling is back as an admission bound: past 64 live workers a
  dispatch is refused by name rather than queued, because a hook waiting behind
  a full cache stalls the tool call that triggered it.
- A large hook payload full of `<`, `>`, or `&` reaches the host again. Those
  three characters were being rewritten as six-byte escapes, so a payload of
  ordinary markup or JSX expanded sixfold against the ceiling one session
  carries: 32 MiB of them claimed 192 MiB of frame. They now travel as
  themselves, and admission is decided by measuring the serialized request
  instead of inferring it from the raw size, so a payload that cannot fit is
  refused by name instead of failing deeper in the transport.

## [12.20.13] - 2026-07-27

### Fixed

- A hook session no longer reports a busy host as an uninstalled one. The
  readiness probe classified every wire handshake failure as "signed host is not
  installed and ready" and prescribed `capt-hook helper install`, so a machine
  under load, where the host serves fine but misses the acknowledge deadline,
  told users to reinstall a healthy deployment. Deadline and timeout failures, a
  peer dropped mid-handshake, and a refusal from a saturated session table now
  say the host is running but slow and that hooks retry on the next event. An
  absent or wrong-build host still points at install, as does a session lost
  after the handshake completed.
- The signed Swift helper is built against the same daemonkit release as the Go
  host again. The generated Xcode project had stayed on daemonkit 0.20.0 while
  the host advanced through the 0.20.x line, so every Swift-side fix below
  reaches the shipped app only now.
- One slow handshake no longer bricks the helper's broker bridge until the app
  restarts: daemonkit 0.20.10's `ServiceSocketClient` stops reading a deadline
  expiry as proof the session ended, so an expiry under load retires that
  generation and the call retries on a successor instead of leaving every later
  probe answering `bridge failed: disconnected`.
- A helper session read that runs out of time reports a timeout instead of
  `errno 35`. daemonkit 0.20.10 surfaces poll-deadline expiry as `ETIMEDOUT`,
  and a `poll(2)` call that itself returns `EAGAIN` retries within the same
  deadline instead of surfacing as a permanent system-call failure.
- Broker-bridge handoff failures land in the helper's file log, not just
  `os_log`: daemonkit 0.20.10 writes that line to standard error too, which the
  helper's launchd plist captures.
- Host logs no longer fill with `peer verification infrastructure failure` under
  load. A peer that exits before code-identity verification finishes is now
  daemonkit's `trust.ErrPeerGone` and takes the quiet path a policy denial
  takes; genuine verifier-infrastructure failures stay loud.

## [12.20.12] - 2026-07-26

### Fixed

- Helper installation now survives Homebrew postinstall's sandboxed temp
  `HOME`: daemonkit 0.20.9 resolves install paths against the real home
  directory instead of the sandbox-scoped one, so `brew postinstall` no longer
  stages the helper into a directory that vanishes with the sandbox.
- `launchctl` exit code 5 is no longer retried as transient. Bootstrap refusals
  surface immediately instead of burning the retry budget on an error launchd
  will keep returning.
- Recovery-mode reconcile clears self-wedged installs: an install the daemon
  itself left half-applied is detected and re-staged rather than reported as a
  conflict on every subsequent convergence pass.

## [12.20.11] - 2026-07-26

### Fixed

- Helper installation no longer wedges in `launchctl bootstrap`: neither the
  `Captain Hook.app` agent nor the `capt-hookd` host agent emits
  `LimitLoadToSessionType`. launchctl refuses some session-keyed plists
  outright, failing every helper reinstall with `Bootstrap failed: 5:
  Input/output error`. The key was redundant for user LaunchAgents anyway, since
  the `gui/<uid>` domain they bootstrap into is already the Aqua session. The
  deployment-identity policy drops its mirrored `session_type` field with them,
  so the first upgrade onto this build re-registers both agents under a new
  policy digest.
- Machines whose prior apply rolled back converge on that upgrade instead of
  refusing it. A failed agent bootstrap leaves a rolled-back apply receipt
  behind, and daemonkit 0.20.8 retires and restages one whose fingerprint no
  longer matches, where earlier releases returned `ErrInstallConflict` on every
  retry.

## [12.20.9] - 2026-07-25

### Fixed

- Package deployment can cold-upgrade past a running build-mismatched host:
  daemonkit 0.20.5 recognizes `launchctl print` exit 113 as not-loaded, so
  stopping the prior host no longer wedges controller recovery during
  `package-install` (previously `recover desired set … process exited with
  code 113` after the required stop).

## [12.20.8] - 2026-07-25

### Changed

- The general pack's models nudges follow the fleet's Opus 5 recalibration
  (2026-07-25): the implementation-spawn and inline-edit nudges now route
  bounded, decision-light implementation to a delegated opus-5 subagent
  (`high` effort; `xhigh` when ambiguous or decision-dense) and reserve
  gpt-5.6-sol for repetitive N-unit sweeps and terminal-heavy execution.
  Review/diagnosis routing is unchanged.

## [12.20.7] - 2026-07-25

### Changed

- Daemon-context CLI discovery now comes from daemonkit 0.20.4's PATH
  inheritance (the daemon extends its own `PATH` once at startup and worker
  children inherit it), replacing 12.20.6's worker-local `augment_path`
  workaround, which is reverted.

## [12.20.6] - 2026-07-25

### Fixed

- LLM-gated hooks and the review judge work in daemon context again: the
  Python product worker now appends the well-known user bin directories
  (`~/.local/bin`, `~/.bun/bin`, `/opt/homebrew/bin`, `/usr/local/bin`) to
  its hermetic launchd `PATH` at startup, so spawnllm's `claude`/`codex`
  probes and hook subprocesses like `gh` resolve — previously every
  LLM-backed evaluation failed with `BackendUnavailable: no installed,
  authenticated LLM backend found` because daemonkit spawns workers with
  `PATH=/usr/bin:/bin:/usr/sbin:/sbin` and forbids overriding it.

## [12.20.5] - 2026-07-25

### Fixed

- Package deployment works on machines with no prior installed generation:
  the deployment bridge probe and the application-stop command now carry an
  exact working directory, so daemonkit's worker validation no longer rejects
  every readiness ping — previously `package-install` always timed out
  (`wait for exact app readiness: context deadline exceeded`) and rolled the
  candidate back, wedging first installs and 12.19.x-to-12.20.x upgrades
  alike.

## [12.20.3] - 2026-07-24

### Changed

- Helper deployment hard-cuts to daemonkit 0.20.1 across the Go module and
  Swift package pins, picking up the tolerate-and-heal handling of stale
  stored launchd programs in daemonkit's service controller.

## [12.20.2] - 2026-07-24

### Fixed

- **Formula publication now distinguishes the user application directory from
  the system directory.** The immutable release guard accepts
  `~/Applications/Captain Hook.app` while continuing to reject
  `/Applications/Captain Hook.app`.

### Changed

- **`Signals.scope` now defaults to `"window"`.** Union scoring across the
  window is the default, so a tell split across adjacent messages accumulates
  toward `threshold`; pass `scope="text"` to require a single message to meet
  it alone. The steering pack's code-stewardship nudge pins `scope="text"` to
  keep its per-message tuning.

## [12.20.1] - 2026-07-24

### Changed

- **The signed desktop runtime is one formula-owned daemonkit deployment.**
  Homebrew stages the notarized app inside the formula and its post-install
  transaction attests, installs, and activates the exact generation at
  `~/Applications/Captain Hook.app`. The standalone cask, `/Applications`
  install, self-managed LoginItem, and client-side daemon spawning are gone.
- **Go and Swift use daemonkit v0.19.1 exactly.** Deployment now seals bundle,
  entitlement, executable, service-plan, runtime-generation, broker-ping, and
  quiescence proofs before an upgrade commits.
- **Hook delivery is observation-only.** Python invokes the deployment-owned
  signed host once and reports an unavailable deployment instead of launching
  or repairing services from the request path.

## [12.20.0] - 2026-07-24

### Fixed

- **A wedged trust-verifier lane now restarts the host instead of rejecting
  every peer forever.** Under heavy load a verifier child's settlement could
  overrun its fixed budget, closing the verifier worker pool permanently while
  the daemon kept serving — every hook on the machine failed as
  `wire: untrusted peer` until a manual restart, which the launchd agent never
  performed because the process stayed alive. daemonkit v0.19.0 makes a
  post-activation lane terminalization fatal: the host exits nonzero, launchd
  relaunches it clean, and the reaper now settles children with an early-settle
  poll instead of sleeping the full termination grace, returning up to 500ms of
  the settlement budget that made the overrun likely.
- **WordNet expansion works from every dispatch thread.** wn pools one
  process-global sqlite connection, created thread-bound by default; the
  worker dispatches on a 16-thread pool, so the first thread to expand a
  phrase claimed the connection and every other thread's NLP signal died with
  `sqlite3.ProgrammingError`, silently dropping steering signals under
  concurrent dispatch. `ensure_wn_lexicon` now opts wn into multithreading
  before the connection exists (safe: sqlite ships serialized,
  `threadsafety == 3`, and lexicon queries are read-only).

### Added

- **New `graphite` builtin pack.** Loads in every repo but stays inert
  outside Graphite ones — each hook bails unless `.git/.graphite_repo_config`
  exists at the git common dir (worktree-aware). In a gt repo it blocks jj,
  steers raw `git commit`/`git push`/branch creation to `gt create`/
  `gt modify` (`ccx vcs ship` preferred), reminds before `gt submit` or
  `ccx vcs ship` until a cc-review pass has run this session (and that PRs
  publish, never draft), and nudges `git rebase`/`git merge`/`git pull`
  toward `gt restack`/`gt sync`.

## [12.19.1] - 2026-07-24

### Fixed

- **Peer verification survives loaded machines.** The trust-verifier child is
  a full process re-exec, and wire's 2s default verify budget left it ~0.5s
  after the worker pool's settlement reserve — on a machine under heavy load
  (an agent fleet, say) every verification timed out and every peer was
  rejected as untrusted. The host now allows verification 10 seconds.

## [12.19.0] - 2026-07-24

### Changed

- **The MCP server launches through the plugin's binrun wrapper.** The
  plugin's `.mcp.json` now runs `bin/capt-hook mcp` — a second binrun shim
  resolving the same version-exact tool environment the hooks use — replacing
  the unpinned `uvx --from capt-hook[mcp]` form that chased PyPI latest and
  held uv's global cache lock for the server's whole lifetime.
- **The `mcp` extra is gone**: the MCP SDK is now a base dependency, so
  `capt-hook mcp` works from any install and both plugin surfaces share one
  materialized environment.
- **The host refuses to start when trust verification cannot work.**
  daemonkit v0.18.0 sizes the trust-verifier lane itself (a product pool
  configuration can never truncate a verdict again) and self-probes the
  verifier exchange before serving, turning the 12.16.0–12.18.3
  untrusted-peer incident class into a loud startup failure.

## [12.18.4] - 2026-07-24

### Fixed

- **The release contract test tracks the corrected tap-publisher pin**, so
  tagged releases pass release-tests again. No runtime change from 12.18.3,
  whose release never published.

## [12.18.3] - 2026-07-24

### Fixed

- **Trust-verifier children can deliver their verdict.** The host's disposable
  worker pool capped child stdout at one byte; daemonkit claims that pool for
  its trust-verifier children, so every verdict was killed mid-write and every
  peer stayed untrusted even after 12.18.1 wired the verifier verb. The pool
  now fits verifier verdicts and verifies connections concurrently.
- **Homebrew publication accepts packaged product helpers.** The release now
  pins the corrected tap publisher, which rejects retired standalone helper
  casks without rejecting `CCNotesHelper.app` inside the supported `cc-notes`
  formula.

## [12.18.2] - 2026-07-24

### Fixed

- **The signed Swift helper now uses the same daemonkit release as the Go
  host.** The helper's generated Xcode project was still pinned to v0.16.0
  after the Go host moved to v0.17.4. Both halves now build against the exact
  v0.17.4 lifecycle and transport contract.

## [12.18.1] - 2026-07-24

### Fixed

- **Peer verification answers daemonkit's trust-verifier child.** The serving
  daemon re-execs `capt-hookd` as a verifier child for every connecting peer;
  `Main` never dispatched that mode, so each child exited 2 ("unknown
  command") and every peer — dispatch, status, stop, and lifecycle alike —
  was rejected as `wire: untrusted peer` from 12.16.0 through 12.18.0. The
  verifier verb now runs `trust.RunVerifierChild`, restoring all traffic.

## [12.18.0] - 2026-07-24

### Changed

- Pin daemonkit v0.17.4 so helper shutdown separates request cancellation,
  product-admission settlement, and terminal transport acknowledgement.

## [12.17.0] - 2026-07-24

### Added

- **Plugin hooks dispatch through a committed binrun wrapper and a dynamic
  descriptor.** Every hooks.json command runs `bin/hook`, a symlink to the
  rendered shim that resolves the `binrun` runner and materializes the exact
  `capt-hook` wheel named by the installed signed host (`capt-hookd version`,
  `build` field). Unpinned `uvx` dispatch is gone, the wheel/app fence cannot
  skew, and the wrapper tree stays out of the wheel and sdist.
- **The host keeps itself current.** An async SessionStart job checks the
  latest release and runs a throttled `brew upgrade --formula captain-hook`
  (formula reinstall repair on failure), posting `update_installed` or
  `update_failed` notifications. `HOOKS_UPDATE_ENABLED` and
  `HOOKS_UPDATE_INTERVAL_MINUTES` govern it; a failure never touches a hook
  dispatch.

### Changed

- **Schema-fenced host stores archive and continue.** The reaper and
  stop-control stores adopt daemonkit's `ArchiveUnsupportedSchema` policy: a
  definitive record-schema mismatch renames the store to a timestamped `.bak`
  and opens fresh instead of refusing to start, while transient errors still
  fail closed.

### Fixed

- **Infrastructure failures in the signed-host client exit 1, never 2.**
  Exit 2 is reserved for a blocking hook verdict alone.

## [12.16.1] - 2026-07-24

### Fixed

- **The tag-bundled release metadata now names daemonkit 0.16.0 exactly.**
  Runtime code and dependency pins are unchanged from 12.16.0.

## [12.16.0] - 2026-07-23

### Changed

- **The signed host and helper now use daemonkit's exact runtime, persistent
  session, disposable-worker, App Group broker, and separated trust-role
  surfaces.** Product-owned lifecycle and the raw helper socket are removed;
  the Go host and generated Swift project both require daemonkit 0.16.0.

## [12.15.3] - 2026-07-23

### Fixed

- **PyPI publication verification now uses the immutable staged package
  manifest.** Publisher-generated attestation sidecars cannot change the
  expected asset set after upload, while the wheel and source archive must
  still match their staged names and SHA-256 digests exactly.

## [12.15.2] - 2026-07-23

### Fixed

- **The final application smoke test consumes the exact bytes verified by draft
  staging.** The staging job carries its downloaded release set across the job
  boundary, eliminating a second private-draft API lookup without weakening any
  hash, signature, stapling, or signed-bridge assertion.

## [12.15.1] - 2026-07-23

### Fixed

- **CI and PyPI release tests now share one cache-backed Python test action.**
  Both restore version-keyed spaCy and Open English WordNet assets, provision only
  on a real cache miss, and run the same development-environment test command.
- **Open English WordNet provisioning no longer depends on en-word.net.** The
  exact 2025+ archive comes from the official GitHub release, is size- and
  SHA-256-verified before installation, and has no registry or fallback path.

## [12.15.0] - 2026-07-23

### Changed

- **The signed helper stack now requires daemonkit 0.10.0 end to end.** The Go
  worker host and generated Swift helper project resolve the same exact hard-cut
  runtime release.

## [12.14.0] - 2026-07-23

### Changed

- **Review persistence now uses cc-transcript's authoritative exact-v1 schema
  engine.** Captain Hook contributes only its product DDL and sqlite-vec extension;
  cc-transcript 14.14.0 compiles and attests the complete database before use.
- **The signed helper stack now requires daemonkit 0.9.0 end to end.** The Go
  worker host and Swift application resolve the same hard-cut runtime release.
- **Widget snapshots now carry an exact caller-owned v1 identity and schema
  fingerprint.** The Python producer, Swift watcher, and WidgetKit reader reject
  identity, version, or shape drift instead of decoding it as current state.

### Removed

- **Captain Hook no longer carries a second schema marker, fingerprint layer, or
  open-time schema implementation.** Foreign, partial, or drifted databases are
  rejected before journal-mode or application-byte mutation.

## [12.13.0] - 2026-07-23

### Changed

- **Review persistence is one exact schema v1.** A component marker, compiled-DDL
  fingerprint, and complete `sqlite_schema` fingerprint fence the base ledger,
  review state, verdict evidence, and sqlite-vec shadow objects as one schema.
- **Shared decisions use cc-transcript's exact v1 store.** Captain Hook now requires
  exactly cc-transcript 14.13.0.

### Removed

- **Open-time schema repair and retired-store probing are gone.** Captain Hook creates
  only an empty review database and rejects every old, partial, missing, or extra schema
  before mutation.

## [12.10.0] - 2026-07-23

### Changed

- **Editing an already-over-budget comment now warns instead of denying.** The `general` pack's
  verbose-comment guard blocks only comment runs an edit genuinely creates — a brand-new over-budget
  run, or a within-budget run grown past budget. Reworking a run that was already over budget before
  the edit draws an advisory instead, so tidying an oversized legacy comment is no longer blocked by
  the act of touching it. Ancestry is position-mapped, so a full in-place rewrite — even a
  delete-and-replace at the same spot — still counts as an edit of the old run.

## [12.9.1] - 2026-07-21

### Added

- **One signed per-user host now owns every hook worker.** `capt-hookd` is an
  exact-versioned universal Go helper embedded in `Captain Hook.app`; it uses
  daemonkit v0.4.2 to own persistent framed Python workers, bound concurrency,
  terminate timed-out process groups, and reap them before returning.
- **The app, helper, wheel, and worker share one exact build identity.** Release
  packaging and signature checks reject architecture, identity, entitlement,
  or version drift before installation.

### Changed

- **Plugin hooks invoke the fixed signed host.** Requests use one exact framed
  protocol and a semantic worker key, so session and account environment cannot
  leak between clients while compatible requests reuse the same warm worker.

### Removed

- **The Python socket daemon is gone.** Per-project listeners, PID files,
  watchdogs, re-exec, fallback dispatch, legacy client grammar, and their tests
  were deleted rather than retained behind compatibility paths.

### Fixed

- **Denied events no longer inherit ordinary warnings.** Warning registrations now
  opt in with `advisory_on_deny=True` only when their advice remains valid after a
  deny, avoiding contradictory output that says a blocked action still runs.

## [12.9.0] - 2026-07-21

### Changed
- **`evt.llm` is the public in-handler LLM surface.** Ask the model from any handler and get a
  typed answer back: a bare prompt returns `str | None`, `bool`/`int` return the parsed answer,
  and a Pydantic model class returns a validated instance; `None` always means the call was
  skipped. `prompt_check` is unchanged and stays public.
- **Builtin packs rewritten onto the current helpers.** Hand-rolled predicate classes collapse
  to `LambdaCondition`, verdict-splatting `message=lambda` callables become `{field}` templates,
  a duplicated condition imports its public counterpart, and the go/python commit gates match
  with `Runs` instead of a raw regex.
- **The fixes pack's command analysis and the rm guard's blast-radius checks now ride the
  fluent `Cmd`/`Call`/`Target` walk.** Three verified divergences, all strictly tighter or
  benign: embedded-quote evasions (`''rm -rf /`) now decline, a wrapper-reached bare `sudo`
  now declines, and git end-of-options `--` spellings git itself refuses to execute are no
  longer flagged. A 46k-case adversarial differential found zero weakenings.

### Removed
- **`llm_evaluate` left the public exports.** It remains the internal engine behind `evt.llm`,
  `llm_gate`, and `llm_nudge`; call `evt.llm` instead.

## [12.8.0] - 2026-07-21

### Added
- **`capt-hook transcripts register` — attach an external transcript to a session.**
  `--session <id>` plus exactly one of `--thread-id` (a codex thread, resolved lazily against
  the codex sessions tree at each dispatch; unresolvable ids drop out silently) or `--path`
  (a transcript file, resolved to itself); `--provider` defaults to `codex`, `--label` is
  optional. Idempotent by provider and locator. The session id names a state directory, so it
  is a trust boundary: ids containing path separators or traversal components are rejected.
- **`capt-hook mcp` — a stdio MCP server exposing `register_transcript`.** The tool takes the
  same arguments and hits the same write path as the CLI. The MCP SDK ships behind the new
  `capt-hook[mcp]` extra; running without it fails with an install hint naming the extra. The
  plugin declares the server via a plugin-root `.mcp.json`
  (`uvx --from capt-hook[mcp] capt-hook mcp`), so plugin users get the tool with no wiring;
  this repo's `.mcp.json` wires the dev venv binary.
- New exports: `register_transcript`, `registered_paths`, `RegisteredTranscript`,
  `RegisteredTranscripts`.

### Changed
- **Registered transcripts fold into the deep view at dispatch.** `lazy_transcript` gains an
  `attach=` callback that resolves registered entries after the loader returns, on the one
  codepath the cold CLI and the daemon share — `evt.ctx.t.deep` now includes registered
  rollouts alongside subagent sidechains.
- cc-transcript pin raised to `>=14.9.0` for the `deep`/`walk`/attachments surface.

### Fixed
- **Stop-time gap sites now look through the deep view.** `EditedSource` reads
  `evt.ctx.t.deep.tool_calls`, and the `subagents=True` recursion behind `ReadFile` and
  `UsedSkill` rides `walk()` instead of a bespoke sidechain scan, so subagent and registered
  codex edits fire the gates that should see them (this repo's own style gate included). Bare
  `tool_calls` is unchanged — recursion stays opt-in.

## [12.6.0] - 2026-07-21

### Changed
- **BREAKING: `scope=` defaults to `"turn"`.** `UsedSkill`, `UsedTool`, and `UserSaid` now
  search only the current turn by default — the directive-and-compliance idiom nearly every
  call site wants; pass `scope="session"` to keep the whole-session scan. The builtin packs
  move with the default: the commit-test gates' `UserSaid("commit", "just commit")` now only
  honors an exemption given this turn, not one from any earlier prompt.

## [12.5.0] - 2026-07-21

### Added
- **`scope=` on the whole transcript-directive family.** `UsedSkill(*names, scope="turn")` and
  `UserSaid(*patterns, scope="turn")` restrict their scan to the current turn, matching the
  `scope=` knob `UsedTool` gained in 12.3.0; the default stays `"session"`.
  `UserSaid(..., scope="turn")` is the declarative spelling of `evt.ctx.turn.matches(...)`,
  so directive-reactive guards no longer need a `CustomCondition`.

## [12.4.0] - 2026-07-21

### Added
- **`Tool.EditTools`** — the prebuilt full edit-shaped set, `Tool("Edit", "MultiEdit",
  "NotebookEdit", "Write")`, so hooks stop spelling the edit tools by hand; the scratch-writes
  approver and `rewrite_code` now use it.

### Changed
- **Review PRs open with an Issue/Fix/Example body.** The session reviewer's PR template
  (`scanning-sessions` skill) drops the Rule/Hook/Evidence sections for three: what went wrong
  (with the strongest verbatim correction woven in), what the hook does, and a short transcript
  vignette of the situation going better with the guard live.

## [12.3.0] - 2026-07-20

### Added
- **`UsedTool` condition** — transcript-history condition matching tool uses by name
  (`UsedTool("Edit", "Write")`; a `|`-joined string still works). `scope="turn"` restricts the
  search to the current turn — the idiom for skipping a guard once the agent has complied.
- **`UserSaid` accepts regexes and clauses, and is exported from the package root.** String
  patterns are case-insensitive regexes (plain keywords keep their substring behavior); `Clause`
  patterns run the dependency-clause scan against each prompt.
- **`evt.ctx.turn.matches(*patterns)` and `evt.ctx.nlp(text, *patterns)`** — public prose-matching
  surfaces taking regex strings or `Clause`s, so hooks no longer import `captain_hook.signals.nlp`.
  `evt.ctx.turn` is now a `Turn`, a one-turn `Session` view with `matches`; `subject_kind` joins
  the exported NLP helpers.

### Changed
- **BREAKING: `Clause.subject` is now a tuple of allowed subject shapes** — `()` (default, no
  constraint), composing the kinds `"unnamed"` (imperatives, pronoun subjects, true passives),
  `"passive"` (a substantive subject without a direct object), and `"actor"` (a substantive
  subject acting on one); a bare string works as singleton sugar. Replaces the
  `"any"`/`"no_nominal"`/`"none"` literals: `subject="none"` → `subject=("unnamed",)` and
  `subject="no_nominal"` → `subject=("unnamed", "passive")`.

## [12.2.0] - 2026-07-20

### Added
- **`Clause(subject="none")`** — vetoes any verb with a substantive active subject, transitive
  or not. `"no_nominal"` only suppresses descriptions with a direct object ("the parser removed
  the node"); `"none"` also suppresses intransitive ones ("the daemon switches to degraded
  mode"), so directive-detection clauses match imperatives and pronoun subjects while ignoring
  described behavior.

## [12.1.0] - 2026-07-20

### Added
- **`evt.llm()` — typed LLM answers as an event method.** `evt.llm("Is this throwaway code?", bool)`
  returns a `bool`; `model=None` returns the raw `str`, `int` an integer, and a `BaseModel`
  subclass its validated instance. Thin sugar over `llm_evaluate`, so contexts, signals, and
  once-per-turn throttling apply; `size=` picks the model tier.
- **`evt.edit` — structural before/after view of the pending edit.** `None` off edit events;
  otherwise an `Edit` whose `.old`/`.new` are lazily parsed syntax trees, with
  `.matches(pattern)` against the post-edit source and `.introduced(pattern)` returning the
  structural matches the edit newly adds.
- **LLM calls retry with validation feedback.** `llm_evaluate` retries a failed backend call up
  to `retries=2` times, feeding a schema `ValidationError` back to the model on re-ask, and now
  raises on final failure; the advisory primitives (`llm_nudge`, `llm_gate`, …) keep their
  fail-silent behavior at the primitive layer.
- **LLM primitive messages splat the verdict.** `llm_nudge(..., message="Do not use Any as an
  escape hatch: {reasoning}.")` substitutes the verdict model's fields into a `{field}` template
  (same placeholder rules as `Prompt.from_template`: only `{identifier}` substitutes, stray braces
  stay literal), alongside the existing literal and callable forms.
- **`LambdaCondition`** — inline conditions from a bare callable:
  `only_if=[LambdaCondition(lambda evt: evt.file is not None)]` wraps the lambda as a
  `CustomCondition`'s `check`, skipping the class declaration for one-off logic.

## [12.0.1] - 2026-07-20

### Added
- **`diff_lint` gains an ast-grep `pattern=` mode.** `diff_lint(pattern="print($$$)", ...)`
  flags only structural matches the edit introduces, wired through `find_introduced`. Match
  identity is the whitespace-normalized text rather than the line number, so a construct the
  edit merely moved never fires — the failure mode of the hand-rolled `lineno`-set diff the
  docs previously showcased. Mirrors `lint(pattern=...)`: violations render as
  `"{snippet} (line N)"`, and `lang` selects both the grammar and the file guard.

## [12.0.0] - 2026-07-20

### Added — fluent command API (`evt.cmd`)

`Cmd`, `Call`, `Target`, `Targets`, and `Expansion` are now public, exported types. `evt.cmd` walks
every invocation in a Bash line — nested `sh -c`/`eval` payloads and command substitutions included —
via `evt.cmd.calls(name)` / `evt.cmd.call(name)`. Each `Call` exposes its operands as `Target`s through
`call.targets`, expands globs under a blast-radius cap with `call.targets.expand()`, and rewrites an
executable in place with `call.sub(old, new)` — subs accumulate on the line and splice back through the
event, so a compound command rewrites atomically or not at all. The rm-guard is built on this surface.

### Changed (BREAKING) — one command surface: `evt.command` is the parsed `Cmd`

The three overlapping command accessors collapse to two names, with no compatibility shim.

- **`evt.cmd` is on every event.** The fluent walker, previously only on `PreToolUse` and
  `PermissionRequest`, is a `cached_property` on every hook event — always a `Cmd`, empty for a non-Bash
  or absent command, so a handler never guards before walking it. `Call.sub` still requires a
  rewrite-capable event and raises on any other.
- **`evt.command` now returns the parsed `Cmd`.** It is an alias for `evt.cmd`, no longer a raw string.
  Read the raw command text with `evt.command.raw` or `str(evt.command)` (`""` for non-Bash). Migrate
  raw-string uses — `in`, `.startswith`, `.split`, `re.*`, `==` — to `.raw`.
- **`evt.command_line` is removed.** The parsed `CommandLine` lives at `evt.command.line`, and its
  fluent query moves from `evt.command_line.q` to `evt.command.q`.
- **`Cmd` gains `.raw`, `str()`, `bool()`, and `.q`.** `Cmd.raw` is the true original command text —
  `str(cmd)` returns it, and it is the operand for raw-text matching (the `Command` regex, `ast_grep`).
  `bool(cmd)` follows `.raw`, so `if evt.command:` and `evt.command or default` keep their empty-command
  short-circuit while non-empty misuse still fails loud downstream; `.q` delegates to the parsed line's query.
- **The `Command` condition now matches lines that parse to zero commands.** A comment, shebang, or
  otherwise command-less line is regex-searched against its raw text — previously skipped, since the old
  `evt.command_line` was falsy when the parse yielded no commands — so a guard blocks in the fail-closed
  direction: `# rm -rf /tmp/x` now matches `Command(r"rm\s+-rf")`.

### Fixed — the rm guard fails closed again

- **Unsafe `rm` rewrites deny instead of slipping through.** The fluent-API rewrite of the rm guard
  dropped its emission-safety gate, so targets carrying `$VAR`, quoted globs, `~`, braces, or
  backslash-newline continuations were rewritten or allowed instead of denied. The gate is
  reinstated: a target that cannot be re-emitted verbatim forces a deny, a line whose command
  substitutions cannot be collected blocks outright, and a tainted `rm` nested inside a
  `sh -c`/`eval` payload blocks as well.

### Changed — dependencies

- `cc-transcript` floor is now `>=14.8`: arity-aware unwrapping, split options, and the payload
  primitives the fluent walker and the hardened rm guard build on.

## [11.0.0] - 2026-07-20

### Changed (BREAKING) — packs are now two providers, zero consumer config

The pack system is redesigned to exactly two providers, and the per-repo pack config is gone. This is
a breaking major: `.claude/capt-hook.toml`, external GitHub packs, and half the `pack` CLI no longer
exist. Migration is mechanical but manual — there is no compatibility shim.

- **Two providers, nothing else.** A pack comes from the `capt-hook` wheel (a builtin) or from an
  enabled Claude Code plugin. GitHub-sourced packs, tarball fetch/cache, pins, and moving refs are all
  removed.
- **No `.claude/capt-hook.toml`.** The per-repo enable list ceases to exist and is no longer parsed.
  Delete it. Builtins activate by policy, not by declaration.
- **Builtins auto-activate.** `fixes`, `general`, `steering`, and `performance` are unconditional in
  every repo; `go` activates on a recursive, non-ignored `go.mod`/`go.work` and `python` on a
  recursive, non-ignored `pyproject.toml`. There are no per-builtin opt-outs — disable the
  captain-hook plugin to turn everything off.
- **Plugin packs load from a fixed path.** An enabled plugin ships zero or one pack at
  `capt-hook/{pack.toml, hooks/}` under its root. Probing, manifest candidates, pointers, and shadow
  resolution are gone. A `capt-hook/` directory that is malformed (missing `pack.toml`/`hooks/`, bad
  descriptor, duplicate tool name) is now a **fatal** load error at dispatch — all-or-nothing, never a
  silent skip. Builtin packs moved from `captain_hook/packs/<name>/` to
  `captain_hook/builtin_packs/<name>/hooks/`.
- **Identity derives from the plugin, not the manifest.** Runtime pack ids are `builtin:<name>` and the
  full plugin id (e.g. `plugin:cc-context@cc-context`); version, description, and repository come from
  `plugin.json`. Authored `name`/`version`/`description`/`hooks`/`nlp`/`marketplaces` fields are gone.
  Claude Code re-lists an enabled plugin once per scope it resolves through, so the roster is deduped to
  one entry per full plugin id by scope precedence (`local` > `project` > `user`); two install paths at
  the same scope is a corrupt roster.
- **`pack.toml` is a tiny descriptor.** It carries only what can't be derived: `resources = [...]` (the
  NLP/tool resources to provision, replacing the `nlp` boolean) and `[tools.<name>]` gate semantics.
  Tool keys are bare tool segments (mount-agnostic); a tool name claimed by two packs is fatal.
- **CLI.** `pack add`, `pack remove`, `pack update`, `pack bootstrap`, `pack scaffold`, and `pack lint`
  are removed. New `pack test <plugin-root>` validates and tests a working-tree plugin pack (folding in
  what `pack lint` did). `pack list` is read-only and lists active builtins plus plugin packs.
  `capt-hook test` now covers only the repo's own `.claude/hooks`.
- **Misfire routing.** A plugin pack's hook-misfire fix PR routes to the `repository` in its
  `plugin.json` (previously dropped); a plugin with no repository is dropped rather than misfiled.

## [10.7.0] - 2026-07-20

### Fixed
- **PEP 723 script fences are exempt from comment-run accounting.** A well-formed `# /// script`
  fence (TOML body, PEP 723 top-level schema) is machine metadata, dropped like a shebang — the
  verbose-comment block, doc warn, and density warn no longer fire on uv script headers. Lookalikes
  (prose body, off-schema keys, unterminated, glued neighbors, non-Python) stay on the plain budget;
  parser bombs classify as non-fences instead of crashing dispatch.

## [10.6.0] - 2026-07-20

### Added
- **Anti-workaround steering hooks** (steering pack 0.8.0). Two hooks now watch for
  consumer-side workarounds of first-party (cc-family) dependencies — the pattern where a
  session papers over a sibling library's missing primitive with local scar tissue instead
  of fixing it upstream. An edit-time nudge (`upstream_workaround_edit`, PreToolUse) fires
  when an edit introduces workaround-flavored comments naming a sibling dependency, and a
  turn-level gate (`upstream_workaround_turn`, Stop) judges the turn's diff and prose,
  blocking when a first-party workaround lands without an upstream fix or a stated
  justification. The gate's prefilter demands two distinct evidence families (workaround
  lexicon, sibling-dep mention, prospective-support phrasing) before the judge runs, and
  both hooks bias toward silence under uncertainty; intentional degradation and
  third-party-dependency accommodations stay unflagged.

### Changed
- **Review persistence is async end to end** — the cc-transcript pin lifts to
  `>=14.6.1,<15`, and `ReviewStore` now composes over `cc_transcript.mining.store.FeedbackStore`'s
  native-async tier (`async` transactions and store calls throughout scan, judge, announce,
  dashboard, notify, pipeline, snapshot, sync, triage, and the decision writer), absorbing
  14.5's persistence-tier inversion and 14.6.1's `run_verdicts` persist serialization.
- **Degraded-call detection reads the upstream `error` field.** `parse_degraded` no longer
  re-parses the tool input to decide whether a call degraded; cc-transcript 14.4+ stamps
  `OtherCall.error`/`FallbackCall.error` at parse time (`None` ⇔ the payload parsed but the
  tool has no typed model), so the ledger's degraded flag is now a field read. One deliberate
  divergence: a non-mapping input to an unknown tool — unreachable through the Claude Code
  hook protocol — now counts as degraded instead of slipping through the old re-parse.
- **Exit-plan reason filtering mints a real synthetic event.** `reason_kept` builds its
  gate event with cc-transcript's `synthetic_user_event` constructor instead of
  hand-rolling a JSON transcript envelope and round-tripping it through the parser — same
  filtering, no consumer-side envelope to drift out of sync with the transcript schema.
  The daemon's incremental transcript cache likewise now cites cc-transcript's documented
  parse-compositionality contract instead of re-deriving it in a comment.
- **Inline tests run under a throwaway `$HOME`.** `capt-hook test` executes each inline test with
  `HOME` pointed at a per-run scratch directory (per-fixture `FileFixture(home=True)` dirs still take
  precedence), so fixtures conditioned on live machine state — configured preferences, real dotfiles
  — behave identically on every machine. Toolchain caches are pinned through the sandbox
  (`XDG_CACHE_HOME`, `WN_DATA_DIR`), so provisioned NLP resources keep resolving; session-replay
  fixtures still look up real local sessions.

### Fixed
- **`capt-hook test` now tests only the project's own surface.** The bare form covers the hooks
  directory plus the packs declared in `.claude/capt-hook.toml`; `--hooks DIR` covers only that
  directory and skips pack resolution entirely. Packs shipped by installed Claude Code plugins are no
  longer swept in — their own repositories test them. Dispatch (`run`, `hooks`, the daemon) still
  discovers everything.

## [10.5.0] - 2026-07-20

### Added
- **Declarative MCP tool specs in pack manifests.** A pack's `capt-hook.toml` may carry a
  top-level `[tools]` table — one entry per bare MCP tool segment, with a required
  `behaves_like = "<builtin>"` and an optional `span_edit` key-map naming the payload's
  `path`/`content`/`delete` keys. Entries parse into `PackManifest.tools` (malformed ones
  raise `PackError` naming the pack and entry) and register with cc-transcript's runtime
  tool-spec registry during discovery, on both the daemon and cold dispatch paths. Gates
  like `Tool("Edit")` now match a declared `mcp__<server>__<tool>`, span-edit payloads
  lower to `SpanEditCall` — so `evt.file`/`evt.content`/`evt.replaced` populate and
  diff-gated hooks fire on MCP edits — and the cc-context ccx pack already ships such a
  table. Registration reconciles by diff (unchanged specs untouched, removed specs
  unregistered) and daemon snapshot hits re-reconcile, so a manifest edit takes effect
  without a daemon restart.
- **Span-edit awareness in the comment hooks.** When no post-image can be simulated,
  `touched()` compares the whole-file pre-image against the new span text — span edits
  only, a conservative superset that suppresses re-introduced blocks and never fires on
  deletions or PostToolUse — so the verbose-comment block now fires on MCP span edits.
- **`install_binary` — a pack provisions its own binary at session start.** The primitive
  registers an async `SessionStart` hook that runs an installer script via `/bin/sh`,
  resolved relative to the calling pack file, with the outcome in `capt-hook logs` — INFO
  on a clean exit, WARNING with a stderr tail otherwise. The handler always allows: a
  missing or failing script is a logged no-op, never a hook failure. Idempotency and
  staleness belong to the script — there is no built-in presence check, so a script that
  short-circuits when the binary is current costs one exec per session.
- **`evt.context(...)` — `additionalContext` without the auto-approve.** On `PreToolUse`,
  `evt.warn` approves the tool call it fires on (`permissionDecision: "allow"`) so the
  warning is delivered without a dialog. `evt.context` builds the same warn-action result
  minus that rider (`HookResult.approve=False`), for hooks that inject context into a call
  they hold no opinion on — steering directives, ambient state. Merging keeps the
  strongest semantics: a warn and a context co-firing keep the rider; contexts alone never
  gain it.

### Changed
- **cc-transcript pins to `>=14.4,<14.5`.** 14.4 carries the tool-spec registry this
  release consumes; 14.5 inverted the persistence tier to native-async and is absorbed
  separately (the pin lifts with that work). Absorbing 14.x also rewrote the review store
  onto `mining.FeedbackStore` composition with a `StoreSchema` (the 14.2 store inversion)
  and moved the judge tier to 14.4's sync surfaces — fixing slug-suggestion retrieval,
  which had been silently failing whenever verdict evidence existed.
- **Language tables are derived from ast-grep's own data, not Pygments.** The build hook now reads
  ast-grep-py's sdist (its `Cargo.lock`, `lib.rs` alias and extension tables, and `parsers.rs`) plus
  each grammar's `node-types.json` from the pinned crate, fetches everything sha256-verified, and
  caches it under `${XDG_CACHE_HOME:-~/.cache}/capt-hook-build`. `LANG_GLOBS` comes from ast-grep's
  extension table and `COMMENT_TYPES` from the grammar node types; both were guessed from Pygments
  before. `pygments` is gone from the build and dev dependencies.
- **`LANG_GLOBS` extension coverage now matches ast-grep.** `.sc` parses as Scala (was Python), and
  `.jsx` as JavaScript — the separate `jsx` key is folded into `js` (same grammar, an upstream alias
  of JavaScript). Bash gains `.bats`, `.cgi`, `.command`, `.env`, `.fcgi`, `.tmux`, `.tool`; C++
  gains `.cu`, `.ino`; TypeScript gains `.cts`, `.mts`; CSS gains `.scss`; HCL gains `.nomad`,
  `.tfvars`, `.workflow`. JSON drops `.jsonl`, `.ndjson`, `.module`, `.xc`, so comment hooks no
  longer fire on JSON-lines files; Elixir drops `.eex`/`.leex`; several rarely-seen extensions leave
  Bash, C++, Python, Ruby, and others.
- **`DOC_COMMENT_KINDS` is generated; `DOC_SIBLINGS` gets its own data module.** The kinds that
  natively mark a doc comment join `LANG_GLOBS` and `COMMENT_TYPES` in the generated
  `captain_hook.langs`, derived from the grammar node types (named kinds pairing `doc` with `marker`
  or containing `documentation` — exact across every grammar, with a fail-loud floor on Rust's two
  markers). Which declaration kinds take documentation comments is ecosystem convention, not a
  grammar fact, so that table moves to the new hand-maintained `captain_hook.doc_conventions`. Both
  tables' values are unchanged (minus the removed `jsx` row).

### Removed
- **The generated `captain_hook/exports.py` is gone.** The lazy-facade name-to-module mapping is now
  an inline literal in `captain_hook/__init__.py`, checked against `__init__.pyi` by a test.

### Build
- **Building from source needs network on a cold cache** (`pypi.org`, `files.pythonhosted.org`,
  `static.crates.io`); a warm cache builds offline. CI caches the derivation directory across runs.
- **The isolated build environment resolves the newest in-band `ast-grep-py`**, so a published
  wheel's `LANG_GLOBS` may gain extensions relative to the lockfile; the supported language set is
  stable within the `>=0.44,<0.45` band.

## [10.4.0] - 2026-07-19

### Changed
- **Verbose-comment budgets now cover documentation comments.** `CommentBlock.too_long` keeps only a
  doc block's opening paragraph carved out, under a 6-line / 400-character ceiling; all trailing
  paragraphs share the plain 3-line / 200-character budget. The new `CommentSegment` and
  `CommentBlock.doc_paragraphs` APIs expose that arithmetic split without changing run or block identity.
- **Doc classification no longer exports the hand-written `DOC_PREFIXES` or `GO_DOC_SIBLINGS` tables.**
  External packs importing those module-level names from `captain_hook.ast_grep` must move to the
  generated `DOC_COMMENT_KINDS` and per-language `DOC_SIBLINGS` tables.

### Added
- **Captain Hook.app — a resident desktop helper** (Swift, on
  [DaemonKit](https://github.com/yasyf/daemonkit)): a Dock-less agent that owns
  review notifications and a WidgetKit PR widget. It serves the frozen
  `~/.capt-hook/helper.sock` v1 line protocol with a per-connection peer check,
  keeps itself registered via login-item reconciliation, and ships as the signed
  and notarized `captain-hook` cask from the same release workflow that
  publishes the wheel. Both sides of the wire are pinned byte-for-byte by shared
  goldens under `tests/fixtures/`.
- **`capt-hook helper install|status|notify`** and a stdlib socket client with
  typed outcomes: socket first, then launch-and-poll the app, then a logged
  drop — never an exception in a hook path, and no osascript fallback.
- **`review snapshot [--refresh]` and `review update --pr-title`.** The snapshot
  command writes the atomic, byte-pinned `~/.capt-hook/status.json` (schema 1)
  the widget reads; candidates carry a PR title, and `pr_open`/`accepted`
  transitions notify through the helper on the winning update only.

## [10.2.0] - 2026-07-18

### Added
- **`rewrite_command_occurrences(visit=...)` — a stateful occurrence walk.**
  Alongside the existing `to=` form (unchanged, and still behavior-identical for
  every consumer), `visit` is called once per occurrence in order — span-less
  ones included — with a `WalkContext` carrying the effective `cwd` (threaded
  through statically resolvable `cd` occurrences), quote provenance
  (`plain_words`), and splice eligibility (`spliceable`). Each call returns a
  `HookResult` to block the whole line (discarding any accumulated rewrites), a
  `str`/`Rewritten` to replace that occurrence (notes deduplicated into one
  `additionalContext` message), or `None` to leave it untouched. `WalkContext`
  and `Rewritten` are exported.

### Changed
- **The general-pack rm guard is rebuilt on `visit=`** and now hard-denies
  catastrophic targets on the macOS rewrite path — the filesystem root, home
  directories, and any directory that contains a git/jj repository (a bounded,
  fail-closed scan). It also descends into `sh`/`bash -c` payloads and `eval`
  arguments (depth-capped, check-only: a risky nested `rm` denies rather than
  rewriting inside a quoted payload), so `bash -c 'rm -rf /'` no longer bypasses
  the guard. The VCS predicates and provenance-safe token emission move to
  `captain_hook.util.vcs` and `captain_hook.util.shell`, shared with the fixes
  pack's danger classifier.

### Fixed
- **The rewrite path no longer downgrades a hard deny to a recoverable trash
  rewrite.** `rm -rf --no-preserve-root /` and `rm -rf ~/Code` were rewritten to
  `trash` invocations (both were hard-denied before the macOS rewrite landed);
  they hard-deny again.
- **Combined shell flag clusters (`bash -lc`, `sh -xc`, …) no longer bypass the
  nested-shell descent** — the payload after any `-…c` cluster is checked, closing
  the gap in the shared classifier too.
- **`cd` cwd-threading is faithful to the shell.** A `cd` to a nonexistent
  directory (the shell stays put) and a `cd` inside a pipeline segment (its cwd
  change does not persist) are no longer threaded, so a later relative `rm` is
  resolved against the directory it actually runs in.
- **Blast-radius tiers normalize `..` before matching**, so `rm -rf /..` and
  `rm -rf ~/..` deny with the correct tier and message.


## [10.0.0] - 2026-07-18

### Changed
- **BREAKING: one `capt-hook.toml`, two sections.** The pack manifest and the
  repo enable-list — previously two conflatable files — unify into a single
  grammar: `[pack]` holds the manifest (`name`, `description`, `hooks`,
  `version`, `nlp`, `marketplaces`), `[packs.<name>]` tables hold enablement
  with the 9.x entry grammar unchanged (empty = builtin, `source`/`commit` =
  GitHub, `disabled = true` = veto). The consumer file moves from
  `.claude/hooks/packs.toml` to `.claude/capt-hook.toml`; the legacy path is
  never read — no warning, no fallback — so a repo can pre-stage the new file
  under 9.x and delete the old one after upgrading. Migration is one `git mv`
  plus wrapping manifest keys under `[pack]`; unknown top-level keys are
  ignored, so a pack-source repo can carry both grammars in one file through
  the transition. A directory is a pack if and only if its manifest has a
  `[pack]` section, so one file can be both a pack and a consumer. Every
  manifest failure mode now raises `PackError` (the bare `KeyError` on a
  missing required key is gone), and `pack add`/`pack remove` preserve a
  coexisting `[pack]` table and its comments.
- **BREAKING: plugin packs are discovered, not attached.** Dispatch enumerates
  enabled Claude Code plugins via `claude plugin list --json` — cached
  per-project in a snapshot invalidated by stat changes to
  `installed_plugins.json` and the three settings files, so the ~1s CLI never
  runs on a warm event — and loads any plugin whose root (or `hooks/` subdir)
  ships a `[pack]` manifest. A declared `[packs.*]` name always beats a
  same-name plugin pack, including `disabled = true` and — fixing a 9.x gap —
  an external that is offline and uncached. Plugin packs now appear in
  `pack list` (kind `plugin`) and load under `capt-hook test`, which the
  session-scoped attach model never allowed. The pack-plugin contract shrinks
  to three artifacts — the `[pack]` manifest, a `plugin.json` dependency on
  captain-hook with a `>=` floor, and the marketplace
  `allowCrossMarketplaceDependenciesOn` entry — with **zero** `hooks.json`
  involvement; `pack lint` enforces exactly that and fails any remaining
  capt-hook `hooks.json` line as predating the contract. A missing or broken
  `claude` CLI degrades to an empty plugin roster, never a per-event penalty.
- **Extra dependency marketplaces ride discovery, every tier.** A
  `marketplaces = [...]` declaration now triggers the bootstrap worker from
  the discovery tail, which covers repo-scoped GitHub packs too (declaring it
  was inert for them before). The worker registers exactly the declared slugs
  — the implicit `yasyf/captain-hook` prepend is gone, since wherever
  discovery runs the dispatcher already exists — an empty union costs zero
  I/O, and the worker spawn is logged rather than printed, keeping dispatch
  output protocol-clean on both streams.
- **`pack scaffold` emits the discovery contract.** Generated packs ship no
  `hooks.json` at all, a `[pack]`-grammar manifest, and a dependency floor of
  `>=10.0.0`.

### Removed
- **`pack attach` and the session-attach machinery** (`attached_packs.json`,
  `AttachedPack`, the four-artifact plugin contract). A plugin still shipping
  the attach line gets a non-blocking SessionStart hook warning under 10.x
  until its cleanup release — its pack loads via discovery regardless. An
  unmigrated repo (no `.claude/capt-hook.toml`) loads zero packs, silently;
  `capt-hook heartbeats` or `capt-hook pack list` makes that visible.
- **The captain-hook marketplace self-bootstrap.** A discovery-era pack plugin
  executes nothing, so the attach-time self-install has no vector; on a
  machine without captain-hook, Claude Code's dependency error is the visible
  signal and `claude plugin marketplace add yasyf/captain-hook` is the
  one-time fix.
- **The SessionStart-subscription lint check.** Discovery happens inside every
  event's dispatch, so there is no attach-vs-SessionStart ordering race left
  to guard; packs may subscribe to `SessionStart`.


## [9.31.0] - 2026-07-18

### Changed
- **cc-transcript 14.** The review/judge stack runs on cc-transcript 14's
  sync, native-core API: the dependency pin is `cc-transcript[judge]>=14,<15`
  (aiosqlite and tree-sitter leave the dependency graph), stores and the
  review pipeline are synchronous end to end with only v14's genuinely async
  surfaces (`run_verdicts`, `record_verdict`, `suggest_canonical_keys`,
  `extract_correction`) still awaited, and scan/session/transcript plumbing
  sits on the v14 `stream`/`discovery`/`resolve`/`capture_window` functions.
- **Protocol markers and tool payloads go through typed surfaces.** The
  conductor classifier, stop-hook feedback fingerprinting, task-notification
  detection, MCP tool-name splitting, and TaskCreate/TaskUpdate prose checks
  now use cc-transcript's filterspec groups, `tools.mcp_parts`, and typed
  tool calls instead of hand-rolled prefixes and raw dicts. Two visible
  semantics notes: stop-marker matching adopts the filterspec-owned regex
  (case-insensitive, no trailing-newline requirement), and structural-noise
  classification stays anchored to the prompt head — protocol tokens quoted
  mid-prompt do not reclassify an authored message.
- **Degraded tool-input parses cover v14's `FallbackCall`.** Inputs outside
  the JSON contract are detected as degraded alongside `OtherCall`.

### Fixed
- **Offline stress harness.** Scenario PR URLs now target the sandbox's own
  repo and the gh stub answers `mergedAt`, so the F08 threshold and F13
  clock scenarios pass offline (pre-existing harness bugs surfaced by the
  migration gates).

## [9.30.0] - 2026-07-18

### Changed
- **Language coverage is generated, not hand-listed.** The nine-language
  `LANG_GLOBS` table is gone; a build hook now probes every Pygments lexer
  against the bundled ast-grep grammars and writes `captain_hook/langs.py`
  (generated, gitignored) with 29 languages and 83 file globs. Comment,
  tombstone, `Pattern`, and rewrite hooks now fire on YAML, JSON (via the
  JSONC-tolerant grammar), Ruby, Kotlin, Swift, C/C++, C#, PHP, Lua, and the
  rest of the probed set — the class of miss where a verbose YAML comment
  went undetected because its language wasn't hand-listed is closed. Known
  tradeoffs, accepted for coverage: an extension claimed by both a supported
  and an unsupported lexer keeps the supported mapping (`*.sc` parses as
  Python), and template dialects parse as their host grammar (`*.eex` as
  Elixir).
- **Comment scanning got correct and bounded.** `COMMENT_TYPES` is now the
  generated cross-language union — `multiline_comment`, `html_comment`, and
  `js_comment` were silently missing — nested comment kinds count once
  (a Dart block comment no longer doubles its own length), sources past
  500 KB skip the scan instead of stalling a hook for tens of seconds, and
  `DOC_PREFIXES` learned the doc dialects of the newly live languages
  (KDoc, Swift/Dart/C# `///`, phpdoc, scaladoc) so their doc comments warn
  as docs instead of blocking as verbose.
- **The public API has one source of truth.** `captain_hook/__init__.pyi`
  (defining-module re-exports) replaces the ~150-entry `_EXPORTS` dict, the
  parallel `TYPE_CHECKING` import block, and the test-side `DEFINING_MODULE`
  map; a build hook parses the stub into generated `captain_hook/exports.py`
  and a 20-line PEP 562 facade serves attributes lazily. Every export
  resolves to the same object as before; bare `import captain_hook` stays at
  its ~3 ms baseline.
- **Condition/event validity lives on the condition classes.** The
  hand-maintained `_CONDITION_EVENTS` map is gone: each restricted condition
  declares `valid_events`, `condition_events()` recurses combinators
  (`And` intersects, `Or` unions, `Not` stays unrestricted), and
  registration errors are unchanged.
- **Skill reference tables are rendered from code.** The events, primitives,
  conditions, and matcher tables in the bundled skills regenerate from the
  live objects behind drift tests; regeneration corrected stale rows —
  `approve`/`deny` still claimed `PermissionRequest`-only defaults from
  before 9.24.0, five primitive signatures had drifted, and `Pattern` and
  `Waiting` had no rows at all.
- **Derived the remaining duplicated tables.** `File.TEST_PATTERNS` covers
  `*_test.py`, `*_test.go`, `*.test.*`, and `*.spec.*`; the wn lexicon
  resolves the latest `oewn` release at provisioning (currently `2025+`)
  and the CI/docs NLP cache keys embed the resolved version; the spaCy
  model name, review status choices, dispatch events, scan detector names,
  judge categories, and review fix paths each have exactly one definition.
  The general pack's inline-edit nudge derives its source globs from the
  generated table (Markdown excluded — doc edits stay inline by policy;
  Zig dropped — no ast-grep grammar).

## [9.29.0] - 2026-07-18

### Changed
- **Recoverable risky `rm` commands move to the macOS Trash.** The general pack
  rewrites outside-repository deletes to the resolved `trash` binary on macOS when
  every literal target, or every match of a glob matching at most 10 files, can be
  verified and safely re-emitted. Oversized or otherwise unverifiable globs and
  repository-root deletions remain blocked on every platform; macOS also falls back
  to the existing deny behavior when `trash` is unavailable.

## [9.28.0] - 2026-07-18

### Added
- **`rewrite_command_occurrences` accepts a callable `block=`.** `block` may now be
  a callable `(evt, cl) -> str` over the event and its parsed `CommandLine`, resolved
  lazily only when the line actually blocks — at the `block_if` hit or the
  zero-rewrite fallthrough, never at registration and never on a successful rewrite —
  so packs can quote the live command in the block message. String `block=` semantics
  are unchanged, including the non-empty registration guard alongside `block_if`.

## [9.27.0] - 2026-07-17

### Fixed
- **Teammate auto-approval no longer trips on repo names like `cc-sudo`.** The
  fixes pack's skip-permissions approvers decided whether to auto-approve a
  subagent's Bash or MCP command with a regex denylist whose `\bsudo\b` branch
  matched the substring `sudo` inside a hyphenated name, so a benign fleet
  enumeration that merely listed or grepped over the `cc-sudo` repo raised a
  permission dialog. Both approvers now parse the command with tree-sitter
  (`cc_transcript.command`) and flag a destructive program only in command
  position: a repo or path name is an argument token, never the executable, so
  that false-positive class is gone. `sudo`, `rm`/`dd`/`shred`/`truncate`/`mkfs*`,
  dangerous `git` subcommands (`reset`/`clean`/`restore`, a force or delete
  `push`), a downloader piped into a shell, and a `sh -c` or `eval` payload
  carrying any of these still fall through to a prompt. The scan stays crash-proof
  against pathologically nested or non-UTF-8 payloads. It remains a courtesy speed
  bump, not a security boundary: consent is granted at launch. The rm-guard's
  `normalize_executable`/`unescape_shell` helpers move to `captain_hook/util/shell.py`
  so both packs share one primitive.

## [9.26.0] - 2026-07-17

### Changed
- **Pack-wide STYLEGUIDE pass.** Single-use module constants inline at their call
  sites across the packs (deletions.py keeps only `GLOB_LIMIT`; glob detection now
  uses stdlib `glob.has_magic`; the fixes-pack scanner inlines its scan cap and
  split regexes), deep nesting flattens through extracted helpers (`handle_rm` in
  the rm guard, `list_leaf_texts` in the fixes-pack payload scanner),
  `CommentDenseEdit` counts via one comprehension, and models.py restores module
  order (helpers before classes) while hoisting the review-routing regex shared by
  two nudges into `REVIEW_ROUTING_PATTERN`. Behavior-preserving except one message:
  the rm guard's "too broad" deny no longer hardcodes the walk-budget entry count.
- **`UserSaid` and `AllEditsUnder` moved to `captain_hook.conditions`.** The
  byte-identical copies in the python and go packs' testing.py collapse into the
  shared conditions module; the packs import them. The scratch-workflow Write
  fixture duplicated between the general pack's review.py and docs.py lives once
  in the pack's `_lib.py`, and teammate_permissions.py derives its nesting-cap
  fixtures from `MAX_SCAN_DEPTH` instead of restating 12/13.

### Fixed
- **rm-guard tests pass on Linux.** pytest's `tmp_path` sits under `/tmp` on
  Linux, so the `SCRATCH_DIR_NAMES` ancestor branch of `is_scratch_path` kept the
  deny tests scratch-exempt even with `TEMP_ROOTS` patched empty — green on macOS,
  red on CI. A shared `no_scratch` fixture now neutralizes both branches, and the
  walk-budget boundary is tested by mocking the `walked_paths` filesystem seam at
  19,999/20,000 entries (with the anchor pinned) instead of monkeypatching a
  since-removed constant.

## [9.25.0] - 2026-07-16

### Added
- **`capt-hook hooks` — the active-hook inventory.** One tab-separated line per
  discovered hook: pack (`local` for a repo-local hook), pack home repo key (`-`
  when unresolvable offline), source file basename, hook name, pipe-joined event
  names, and the first line of the message or handler docstring. No tests execute,
  and discovery resolves packs exactly as `capt-hook test` does. The
  session-reviewer brain's new overlap check reads it; the format is pinned in the
  scanning-sessions references and changes stay additive.
- **authoring-hooks EXTEND mode.** The third drafting shape: broaden an existing
  hook to cover a newly mined rule — one new firing test plus a benign-neighbor
  `Allow()`, every pre-existing test untouched, message string byte-identical.
- **Release workflow: plugin.json auto-sync.** A `sync-plugin-version` job commits
  `captain_hook/.claude-plugin/plugin.json` back to `main` with its `version` set
  to the released tag — the bump consumer plugin caches refresh on, previously a
  manual step that was forgotten for six straight releases (and again for 9.24.0).
  Runs parallel to the GitHub-release job; a failed push turns the release run red
  without blocking the published artifacts.

### Changed
- **Session reviewer: PR routing follows the change shape, not the candidate
  kind.** Every create candidate now takes a mandatory overlap check against the
  repo's active hooks before drafting: a rule an active hook already covers is a
  logged skip; a universal broadening of a pack hook becomes an edit PR'd against
  the pack's own repo (uncertain → repo-local, and repo-specific or
  preference-shaped rules always stay repo-local); only genuinely new hooks land
  as new `.claude/hooks/` files. Fix-kind scan-time routing — the `routing:` line
  and `repo_key`/`origin_repo_key` semantics — is unchanged.

## [9.24.0] - 2026-07-16

### Changed
- **`approve()` and `deny()` now register on `PreToolUse | PermissionRequest` by
  default.** A `PermissionRequest`-only hook is silently dead on the
  forwarded-teammate-dialog path (CC #73176 — zero `PermissionRequest` hooks run
  there), which made the old default a trap for every pack author. The
  `PreToolUse` half resolves upstream of that fork, so bare `approve()`/`deny()`
  hooks — including external packs like cc-notes' approvers — now cover teammate
  and subagent dialogs with no call-site change. Explicit settings `deny`/`ask`
  rules still win — Claude Code evaluates them regardless of a `PreToolUse`
  allow — so the flip only skips prompts those rules wouldn't force; pin
  `events=Event.PermissionRequest` to answer only dialogs that actually appear.
  `llm_approve()` gains the same `events=` parameter but keeps the
  `PermissionRequest`-only default — `PreToolUse` fires on every matching call,
  which would put the judge's LLM round-trip on the hot path.
- **Fixes pack: subagent auto-approve under skip-permissions now covers every
  tool, MCP included.** The teammate hook was native-Bash-only by design;
  a second disjoint hook approves all other tools from subagents/teammates when
  the session was launched with bypass available
  (`--dangerously-skip-permissions` or `--allow-dangerously-skip-permissions`).
  Courtesy guards remain: the Bash command denylist scans every string in the
  tool input (top-level values and list items, so `{"cmd": ...}`,
  `{"script": ...}`, and argv-shaped lists are covered), and MCP tools whose
  name carries a destructive verb token — split on case and punctuation
  boundaries, sixteen verbs from delete to terminate — still prompt.

### Fixed
- **Dispatch: a rewrite now beats a plain allow.** The first allow-or-rewrite
  used to win, so a broad approve registered ahead of another pack's rewrite
  dropped the corrected input and executed the original call. Composition
  precedence is now block > rewrite > allow; among rewrites the first wins.

## [9.22.0] - 2026-07-16

### Added
- **A pack self-registers its extra dependency marketplaces.** A pack-shipping
  plugin's `capt-hook.toml` gains a `marketplaces` list of `owner/repo` slugs.
  On the first session that finds one unregistered, `pack attach` runs `claude
  plugin marketplace add` for it — alongside captain-hook's own — in the
  background worker, and Claude Code resolves the matching `plugin.json`
  dependency. A plugin with several cross-marketplace dependencies now keeps a
  one-line install: the user adds only the plugin's own marketplace, and the
  rest register themselves. Introduced in 9.21.0, reliable here.

### Fixed
- **Generalized marketplace bootstrap holds up across source types and configs.**
  A marketplace registered by git URL or local path now counts as known, so
  bootstrap no longer loops hourly re-adding an already-registered marketplace;
  one failing marketplace no longer aborts the rest of the list; `pack attach`
  never blocks SessionStart on the worker's lock; per-marketplace attempt
  markers are keyed per config dir, so two `CLAUDE_CONFIG_DIR`s sharing a state
  dir stop damping each other; and repo-slug validation is ASCII-anchored.

## [9.21.0] - 2026-07-16

### Fixed
- **Blocking gates enforce on every agent type again.** Since the
  `skip_planning_agents` default landed (`True`, 8.15.0), any gate firing on
  `SubagentStop` was silently suppressed for `Explore`/`Plan`/`general-purpose`
  subagents — and `general-purpose` is the default delegated-implementation
  subagent, so a gate's only firing path for delegated work went dead. The flag
  now defaults to `None`, resolving to `not block`: blocking gates (`gate`,
  `llm_gate`, `hook(block=True)`, and the `workflow()` guard) enforce on every
  agent type, while warning nudges keep skipping planners. Restores the steering
  pack's deferral/downgrade `llm_gate` on delegated subagents. Regression tests
  pin gate/nudge/hook/llm/workflow behavior at `SubagentStop`.

## [9.20.0] - 2026-07-16

### Fixed
- **Resumed sessions no longer inherit stale skip-permissions consent.** The
  resident daemon cached the launch-flag walk per session ID, but session IDs
  survive `claude --resume` — a session relaunched without
  `--dangerously-skip-permissions` kept serving the old consent verdict (and
  vice versa). The cache is gone: bound requests walk the client's process
  tree fresh on every call, while `BaseHookEvent.skip_permissions` still
  memoizes within a dispatch, so per-event cost is unchanged.

## [9.19.0] - 2026-07-16

### Fixed
- **General pack: the review-routing nudges honor the fable escalation lane.** The
  two LLM-judged review-routing nudges fired on sanctioned escalations — a
  conditional fable fallback reached only after a codex-wrapper stage returned
  nothing (4 false fires in one live session), and a review run directly on fable
  under a declared, documented codex-wrapper failure ("sol lane quota-dead;
  escalation per models table") the stateless judge cannot verify. Both judge
  prompts now carve those out, scoped tightly after adversarial review: the
  declaration covers only the stages doing the declared work, a stage gated on
  anything but a codex-wrapper miss is not a fallback, and the retired wrapper
  shape stays fired even inside fallback branches. Regression tests pin both
  carve-outs and all three counter-shapes, and the nudges now carry stable
  `label=` identities so future prompt edits keep fire state and complaint
  attribution. Validated live: 4 sanctioned-escalation shapes silent, 5 misroute
  shapes still fire.

### Added
- **General pack: risky `rm` blocking.** A new `deletions` hook denies Bash `rm` when a target resolves outside any git/jj repository (temp roots and scratch-named dirs exempt via `is_scratch_path`, extracted to `captain_hook/util/scratch.py` as the shared scratch seam), when a glob argument matches more than 10 files (bounded expansion — never a full listing), or when a literal target is itself a repo root. `sudo`/`env` wrappers unwrap; `git rm`, `xargs rm`, and command substitutions pass through. Adversarial-review hardening preserves leaf-symlink semantics, validates glob matches individually, tracks preceding `cd`, and budget-caps recursive `**` scans.

## [9.16.0] - 2026-07-15

### Added
- **`review slots [--repo <key>]`.** The open-PR cap check: prints
  `<repo>: open_prs=<n>/<max> free=<free>`, counting live open PRs by the repo each
  PR targets, and exits 1 when full. The scanning-sessions brain runs it immediately
  before `gh pr create`, so a cap that filled mid-pass becomes a logged skip instead
  of an over-cap PR.
- **Auto-enrollment.** `review run` enrolls a repo it has never seen as watched, so a
  plugin-wired repo is reviewed from its first session end with no manual
  `review enable`; an explicit `review disable` still sticks.
- **Unwatched-session canary.** `capt-hook status` names repos whose recent sessions
  ended unwatched with no watching record — a line that self-clears as
  auto-enrollment picks those repos up.

### Changed
- **The open-PR cap counts the repo each PR actually targets**, parsed from the
  candidate's PR URL (distinct URLs; URL-less rows fall back to the candidate's
  repo), instead of the candidate's origin repo. Cross-repo pack-fix PRs now consume
  the pack repo's slots rather than the origin repo's, dashboards report the real
  per-repo open-PR count, and `review update` rejects a malformed `--pr-url` at the
  boundary. `--repo` filters normalize case.

### Removed
- **The generic-create route to captain-hook's general pack.** A create candidate's
  PR always opens against the repo it was observed in; only fixes to existing pack
  hooks target the pack's own repo. The `seen_in_repos` line on `review show` and
  `ReviewStore.cross_repo_rules` are gone with it.

## [9.15.0] - 2026-07-14

### Added
- **`rewrite_command_occurrences` primitive: per-occurrence Bash-line rewrites.** `to(evt, occ)`
  runs once per parsed command of a `;`/`&&`/`|`-joined line, and every non-`None` return is
  spliced back by byte span, so untouched segments, operators, redirects, and comments survive
  byte-for-byte. `block_if` (which requires a non-empty `block`) checks every occurrence before
  any rewrite and blocks the whole line when one trips; a callable `note` composes one message
  from all `(Occurrence, replacement)` pairs. `Occurrence` is re-exported at the root. Raises
  the cc-transcript floor to `>=13.2` for `Command.span` and `CommandLine.splice`.
- **Inline-test fixtures for path-shaped guards.** A `{file}` token in `Input.command` is
  replaced with the materialized `FileFixture`'s absolute path, and `FileFixture(home=True)`
  materializes under a private temp home with `$HOME` swapped for just that test, so stat- and
  `expanduser`-based command guards test deterministically.
- **Fixes pack: scratch-dir writes stop prompting under bypass consent.** A new
  `scratch_writes` hook auto-approves native file-tool writes (`Edit`/`Write`/
  `MultiEdit`/`NotebookEdit`) whose target resolves into a system temp root
  (`/tmp`, `/private/tmp`, `/var/folders`, `/dev/shm`) or a
  scratch-named directory, in sessions launched with
  `--dangerously-skip-permissions` — plan mode's permission override and
  forwarded subagent dialogs otherwise pop dialogs for consent the launch flag
  already gave. Fires on `PreToolUse` and `PermissionRequest`, like the
  teammate-Bash hook and for the same forwarded-dialog reason. Relative targets
  resolve against the payload `cwd`, so `../../tmp/...` spellings approve while
  `/tmp/../<elsewhere>` spoofs and symlinked scratch-named directories are judged
  by where they actually land; MCP `mcp__<srv>__Write` lookalikes are vetoed.
- **`evt.cwd`.** Every event exposes the payload's working directory as a
  `Path | None`, and inline tests seed it via `Input(cwd=...)`.
- **`pack attach` self-bootstraps the captain-hook marketplace.** A consumer
  plugin's first session on a machine without the `yasyf/captain-hook`
  marketplace registers it and installs the captain-hook plugin in a detached
  background worker, then prints one notice line — so a pack-shipping plugin no
  longer tells users to add the marketplace by hand. The bootstrap is
  idempotent (a registered marketplace short-circuits it with one file read),
  hourly-damped on failure, and launches its worker isolated (`python -I`) with
  an absolute `claude` path.
- **`pack scaffold` generates a pack-shipping plugin's artifacts.** `uvx
  capt-hook pack scaffold <dir>` writes `capt-hook.toml`, an attach-only
  `hooks/hooks.json`, the `plugin.json` captain-hook dependency, the
  `marketplace.json` cross-marketplace allowlist, and a starter hook, then runs
  `pack lint`. It repairs a partial pack in place — adding only missing contract
  pieces and migrating legacy mirrored `capt-hook run` entries (canonical or
  bare-`uvx`) to the single canonical attach — and never rewrites a conforming,
  unparseable, or unclassifiable file, so it can't clobber content it doesn't
  understand.

## [9.14.0] - 2026-07-14

### Changed
- **General pack 0.22.0: implementation routing splits on decision density.**
  The implementation-spawn and inline-edit nudges (messages and classifier
  rubrics) now send bounded, decision-light work — the plan, work order, or
  repeated pattern already made the decisions and what remains is execution —
  to gpt-5.6-sol via the codex:codex-wrapper agent, and ambiguous,
  decision-dense, or long-run implementation to opus at xhigh. This replaces
  the "well-scoped, clearly-bounded" criterion and matches the fleet Models
  table v5, which also notes sol's two lane gates: judgment degrades as
  mid-task decisions pile up, and surrounding-code conventions are matched
  less faithfully (backstopped by finder/refuter review plus capt-hook
  guards).

## [9.13.0] - 2026-07-13

### Removed
- **The duplicate-dispatch once-guard is gone.** The shim existed solely to collapse
  the sibling processes a legacy consumer plugin's mirrored `run` entries caused
  (Claude Code dedupes hook commands per registering source, not globally). With
  every consumer on the attach-only pack contract and legacy plugin caches verified
  decayed fleet-wide, there is no mirroring source left to collapse — dispatch now
  runs unconditionally on both the cold CLI and daemon paths. The
  `CAPT_HOOK_ONCE_TTL` knob is removed with it. A mirrored `run` entry that
  resurfaces (a hand-wired settings line, a pre-contract plugin cache) now
  double-fires side-effecting hooks; the fix is deleting the extra registration,
  per the troubleshooting guide.

## [9.12.0] - 2026-07-13

### Added
- **Dispatch heartbeats — `capt-hook heartbeats --session <id>`.** Every event now writes
  an unconditional per-`(session, event)` beat at dispatch entry, before matching, so "the
  event never dispatched" is distinguishable from "it dispatched and matched nothing" — a
  gap the decision ledger (which records only hooks that *fired*) can never show, and the
  one that cost hours when a teammate's `PreToolUse` silently never reached dispatch. The
  new command lists a session's per-event coverage; a missing event is a wiring gap, not a
  quiet session. Backed by cc-transcript 13.1.0's `HeartbeatLog` (a `dispatch_heartbeats`
  table in `decisions.db`), written straight through as one indexed upsert on the sync
  dispatch path.

## [9.11.0] - 2026-07-13

### Added
- **Opt-in resident daemon: warm hook dispatch, ~7x faster.** A per-project `hookd`
  worker keeps the interpreter, imports, and discovered hook registry resident and
  serves events over a Unix socket; the new stdlib-only `hook` console script sits
  in the wired hook-command slot, forwards `run <Event>` to the worker (spawning it
  on first use), and passes every other invocation through to the cold `capt-hook`
  CLI untouched. Measured dispatch drops from ~490ms cold to ~70ms p50 warm. Warm
  output is byte-identical to cold — stdout, stderr, and exit code — enforced by a
  12-case parity suite that runs as the release's acceptance gate. When a worker is
  unreachable the client falls back to running the hook cold
  (`CAPT_HOOK_DAEMON_FALLBACK`, with `open`/`closed` modes and a strict send-boundary
  rule so a hook never double-fires: a pre-send failure reruns cold, a post-send
  failure fails open); `CAPT_HOOK_NO_DAEMON=1` bypasses the daemon entirely. One
  worker serves each project root across sessions and pooled accounts, idle-exits
  after two hours, and restarts itself when the installed build changes; the
  `hookd status|stop|restart|logs` ops surface inspects and drives workers without
  ever spawning one. Shipped after a multi-pass adversarial review
  plus a security pass: 0600 socket in a 0700 run dir, peer-uid verification on both
  sides, and untrusted run dirs always served cold. Opt-in per project and dogfooded
  in this repo; plugin consumers keep the cold `uvx --isolated capt-hook run` wiring
  unchanged. The new Resident daemon guide page covers enabling it, the fallback
  model, the env knobs, and the accepted limitations.

## [9.10.0] - 2026-07-13

### Changed
- general pack: the verbose-comment rule is now a blocking hook — an Edit/Write/MultiEdit that leaves a comment run (or blank-line-separated comment block) over 3 lines / 200 chars is denied. Size is measured on the post-edit text for runs the edit created or modified; untouched legacy runs stay exempt (multiset-keyed, whitespace-reflow-aware). Doc-generation comments (godoc — including grouped const/var specs, struct fields, imports, interface methods — rustdoc, JSDoc) are exempt from the block and warn instead; a run is doc only when every line carries the doc marker. New comment-density advisory: an edit whose added lines are mostly comments (≥6 added, >50% non-doc) warns. Trailing comments group as singletons; shebang lines don't count.
- dispatch: a deny no longer discards other hooks' advisories — declarative warns accumulate under "Additional advisories (not the reason for the deny):". Handler-backed (LLM/async) hooks are skipped once a block has fired, so they no longer spend cost or max_fires budget on a denied call.

### Added
- ast_grep: comment-run machinery (CommentRun, CommentBlock, comment_runs, comment_blocks, touched_comment_blocks, comment_line_numbers) with framework thresholds MAX_COMMENT_LINES=3 / MAX_COMMENT_CHARS=200, importable by external packs.
- events: pre_image/post_image full-file images for Edit/Write/MultiEdit on PreToolUse.
## [9.9.0] - 2026-07-13

### Changed
- **Hook dispatch now follows deny-wins precedence.** `dispatch()` returned on the
  first decisive result, so an `allow` from an earlier-loaded hook short-circuited a
  later hook's `block` — e.g. the `fixes` pack's teammate-bash allow suppressing the
  `general` pack's `git stash` / `jj undo` guards. A `block` from any matching hook now
  beats an `allow`/`rewrite`, matching Claude Code's own `deny > ask > allow`; among
  approvals the first still wins and `warn`s still accumulate. Every matching hook now
  runs (there is no short-circuit on an approval), so a later block is always seen.

### Fixed
- **Teammate Bash auto-approve now works on the forwarded/resumed permission path.**
  The `fixes` pack's `approve_teammate_bash_under_skip_permissions` hook answered only
  `PermissionRequest`, but Claude Code forwards an in-process teammate's permission
  dialog to the lead when the teammate's `ToolUseContext.requestDialog` is absent
  (resumed/rehydrated sessions) and runs no `PermissionRequest` hooks on that path — so
  the auto-approve silently never fired there, and the forced multi-`cd` "for clarity"
  prompt sat unanswered. The hook now also answers `PreToolUse`, which resolves upstream
  of the forward fork and auto-approves on every path. `approve()` gained an `events`
  parameter (default `PermissionRequest`) to opt into this.

## [9.8.0] - 2026-07-12

### Added
- **`capt-hook pack lint <plugin-root>`** vets a pack-shipping plugin against the
  attach-only dependency contract. It checks that the `capt-hook.toml` manifest
  resolves; that `hooks.json` carries exactly one canonical SessionStart `pack
  attach` entry — the dir arg double-quoted as `"${CLAUDE_PLUGIN_ROOT}"`, or its
  `hooks/` subdir, to match the manifest layout — and nothing else that invokes
  `capt-hook`; that `plugin.json` declares the captain-hook dependency as an object
  with `marketplace` `"captain-hook"` and a `>=X.Y.Z` version floor; that the repo's
  `marketplace.json` allows the cross-marketplace reference (WARN, not a failure,
  when absent); that the pack loads at least one hook with no load errors; that the
  pack subscribes no SessionStart events (attach and the canonical run SessionStart
  are unordered siblings); and that no hook registers `async_=True` on a
  decision-capable event. Each check reports pass, warn, or fail with a reason; any
  failure exits non-zero.

### Changed
- **The Claude plugin manifest now carries a `version`.** Dependency ranges can
  resolve against it, but this flips the plugin cache from SHA-keyed to
  version-keyed: every future plugin-content change must bump the version or
  consumers keep running the cached build.
- **Registering an `async_=True` hook on a decision-capable event now raises.**
  Claude Code never awaits a background hook's stdout, so an allow/deny/block
  verdict returned by an async gate on PreToolUse, Stop, SubagentStop, or
  PermissionRequest was silently discarded. The registration path rejects the
  combination with a clear error instead of shipping a gate that never fires.
- **Attached packs resolve in stable name order, and a same-name re-attach from a
  new dir logs a WARNING.** Sorting the attached tier by name means gate
  arbitration no longer depends on attach timing. A pack re-attaching from a
  different dir still wins as the newer attach — a plugin update bumps its
  versioned cache path, so erroring would drop the pack for every post-update
  session — but the rebind is now logged at WARNING naming both the old and new
  dir, so a genuine two-plugins-one-name clash is visible.

### Fixed
- **Two packs' same-named hooks no longer share a `max_fires` counter.** Per-hook
  session state (the fire-count ledger backing `max_fires`) keyed on the bare
  function name, so a plain `@on` handler named `check` in one pack and another
  in a second pack collided — one pack's single fire could suppress the other's.
  The state key is now namespaced by the hook's defining file, which is unique
  per pack. Upgrade note: the on-disk state-key format changed, so an in-flight
  session's already-fired one-shot (`max_fires`) hooks may fire once more right
  after the upgrade — a one-time transient as the new keys take over, not a
  persistent double-fire.
- **The duplicate-dispatch guard stays fail-closed.** The `once_guard` shim now
  centralizes the `DECISION_EVENTS` exemption for both the CLI and daemon
  dispatch paths, but keeps the deliberately dumb contract: the first
  byte-identical sibling to claim wins, and a claim whose dispatch raises stays
  held for the TTL window rather than releasing. Releasing would re-run a legacy
  sibling whose earlier hooks already completed their side effects, or unlink a
  claim a slower sibling re-took after the TTL — there is no ownership check.

## [9.7.0] - 2026-07-12

### Added
- **Moving packs show what they resolved to.** `@latest` and bare-source pack
  resolution now persists the resolved GitHub release tag (or branch) in the
  pack's per-machine sidecar; `pack list` shows that ref for moving GitHub
  packs instead of the manifest's often-stale `version`, falling back to the
  manifest when the sidecar predates 9.7.0; and `pack update` echoes
  `updated <name> -> <ref>@<sha>`.

### Changed
- **A pack manifest's `version` key is optional** and defaults to `0.0.0`.
  Keep the key while pre-9.7.0 capt-hook is in the wild, though — older
  releases crash on a manifest without it.

### Fixed
- **Hook dispatch no longer runs a stale `uv tool install capt-hook`.** A
  machine with any prior unpinned tool install had every bare `uvx capt-hook`
  hook invocation silently short-circuit to that installed environment —
  uv-documented behavior — freezing hooks at whatever version the install
  left behind. The canonical prefix is now `uvx --isolated capt-hook` across
  the plugin's `hooks.json` and every agent-executed skill command: installed
  tools are ignored without forcing a network refresh, so dispatch works
  offline and ephemeral-cache revalidation still picks up new releases about
  same-day, at ~135ms median added per dispatch.

## [9.6.0] - 2026-07-12

### Added
- **The fable main loop is nudged to delegate sustained browser automation
  (general pack 0.19.0).** A new `PostToolUse` `llm_nudge` fires when the main
  loop (never a subagent) drives a run of `agent-browser`/`playwright` calls
  inline — five or more browser tool calls via Bash or Skill within the
  current turn — and steers the hands-on browser work to a delegated `opus`/`xhigh`
  teammate (or an `agent-browser-with-cookies` teammate when the site needs the
  user's login), just like any other implementation. A single gated, stateful,
  or authenticated interaction the agent just decided to run (a go/no-go
  verification, a login+2FA flow) stays inline: an LLM judge declines below
  that bar, and the nudge fires at most once per session.

## [9.5.0] - 2026-07-12

### Added
- **The first turn batch-loads the always-used deferred tools (general pack
  0.18.0).** A new `tools` SessionStart nudge injects additionalContext telling
  the agent to load the task/plan/monitor/message tool schemas in one
  `ToolSearch select:` call rather than paying a lookup round-trip per tool as
  each is first needed; names already resident or absent in the running version
  simply don't match, and every other deferred tool stays unloaded until a task
  needs it. It fires once, only on a fresh `startup` or `clear` — `resume` and
  `compact` keep the schemas already loaded — and never for a subagent. The
  inline `Input(source=...)` test surface now threads `agent_id` into
  SessionStart events so the subagent-skip case is simulable.

## [9.4.0] - 2026-07-12

### Fixed
- **The pre-existing-issue steering nudge stops firing on itself.** The
  predicate double-counted "pre-existing" (a regex signal and an NlpSignal both
  scored the same phrase, reaching the threshold alone), WordNet expansion of
  "issue" over-matched everyday nouns like "results", and the leave clause
  matched past-tense completion reports ("left the flaky one documented").
  The adjective signal is merged into the regex, the nouns are a curated
  literal set, and the leave clause requires prospective tense. The message
  string is byte-identical, so `nudge_1ebed8c4` keeps its fire history and
  attribution; a ten-case inline regression matrix pins the four misfire
  classes reproduced live plus the 8.18.0 origin/echo mechanisms. Steering
  pack 0.7.0.
- **Recurring misfires of an already-"fixed" hook re-propose the fix.** Fix
  candidates are unique per hook, so once a fix PR merged, new judge-confirmed
  misfires attached as observations to a closed candidate and died there
  (candidate #6 collected two post-merge confirmations at 0.98/0.88 and stayed
  ineligible forever). Accepted fix candidates now reopen to watching with a
  generation bump when a judge-accepted observation lands after `resolved_at`
  (stamped on acceptance; merged-PR sync backfills it), counting only
  post-resolution evidence toward thresholds — a single strong-marker
  recurrence re-qualifies.
- **Sessions that never end get reviewed.** The reviewer was SessionEnd-only,
  so a complaint in a long-lived session sat invisible for days. Ending any
  session now sweeps the repo's still-open sibling transcripts through the
  mtime-watermark scan, and the plugin's `hooks.json` runs the same detached
  `review run` on SessionStart for the overnight case.
- **Complaints that name a hook attribute without a fingerprint.** Attribution
  required a fire fingerprint within three turns; "the task-tracking hook keeps
  firing" with no nearby fire never ingested. A named-hook fallback matches a
  unique decision-ledger kind within thirty minutes (failing closed on
  ambiguity), and the strong-marker vocabulary gains verb-anchored
  incorrectly/mistakenly/erroneously × fired/triggered/flagged.

### Added
- **Pack hooks have a continuous-improvement story.** Misfires of pack hooks
  route their fix candidates to the pack's home repo (builtins to captain-hook,
  cached GitHub packs to their own repo — network-free, cache-miss falls back
  to repo-local) while staying visible from the observing repo: candidates
  carry `origin_repo_key`/`pack_name`, every listing matches either key, and
  eligibility gates on the *origin* repo's watching flag. The scanning brain
  learned the three-way dispatch — repo-local worktree, builtin-pack clone
  verified with `uv run --project . capt-hook test`, external-pack clone with
  `uvx capt-hook --hooks <dir> test` — and never falls back to committing a
  pack fix in the watched repo.
- **CREATE candidates classify before the PR opens.** Generic behavioral rules
  (the "wait for plan approval" shape) PR against `captain_hook/packs/general/`
  in a captain-hook clone instead of the watched repo; `seen_in_repos: N` in
  `review show` (rules observed across repos) feeds the call, and repo-specific
  rules stay in `.claude/hooks/`, the default when uncertain.
- **You hear about reviewer PRs.** In reviewer-wired repos, SessionStart
  surfaces unannounced PR lifecycle changes as additionalContext one-liners
  (cross-repo lines name the pack and target repo; merged/closed/stale get
  follow-ups, each announced once), and the brain fires a macOS notification
  when it opens a PR.
- **`hook()`-authored hooks are attributable.** They register as
  `<stem>:hook_<sha8>` with the caller's real file instead of `declarative_N`
  with an empty source path, external-pack modules get pack-qualified stems,
  and wheel/pack-cache sources resolve to module stems at attribution like
  primitives always did. Old `declarative_N` ledger rows stop resolving
  (precision over recall).
- **Junk CREATE candidates get filtered before they burn judge calls.** Six
  deterministic classes (teammate relays, agent-stop notices, @path handoffs,
  limits-reset notices, bare plan-approval go-aheads, shell-command leads) and
  paste-only quote/fence events drop at scan — a measured 29% of the historical
  rejected-create corpus with zero false drops, junk-lead-with-real-tail
  preserved. Survivors get a keep-biased small-tier LLM triage inside the
  already-detached review spawn, recorded per dedup key so nothing re-triages;
  the judge stays the backstop for everything kept.
- **The brain run is observable.** `spawn_brain` returns exit code, duration,
  and log path; the report records PRs the run opened plus per-sync
  merged/closed/kept counts; the status health line renders
  `brain: exit 0 · 142s · 1 PR` and goes red when eligible work produced no PR
  — the silent-failure surface that hid keychain and text-only-reply deaths.
  Every PR-state sync transition is logged with its provenance
  (`gh` now reports merged-at, which backfills `resolved_at`).

### Changed
- **`capt-hook status` renders in under a second.** Previously 16.4s on a
  live-size database locally — and minutes through a cold `uvx` plus gh
  round-trips: the backlog count no longer probes transcript hydration per row
  (cc-transcript 10.8.0's `probe_hydration=False`, with by-UUID discovery
  memoized upstream), `overview()` collapses its per-candidate N+1 into
  set-based queries (`crosses_thresholds` remains the sole eligibility
  predicate), gh PR states cache in a `pr_states` table with a 15-minute TTL
  (`review sync-prs` forces refresh; gh-down serves the last known state),
  `purge_stale_verdicts` runs only when the prompt fingerprint changes
  (store reopen ~2ms), and pack import no longer loads WordNet or spaCy —
  `Phrase.expand` is lazy until a predicate actually runs. The dashboard also
  collapses the rejected wall to a count line beyond five entries.
- **`Clause.completed` is now `Clause.tense`.** The boolean became
  `"any" | "completed" | "prospective"` — "prospective" rejects past
  predicates, which the boolean could not express (and excludes
  counterfactual modal-perfects like "should have left"). `completed=True`
  maps to `tense="completed"`; the default is unchanged in behavior. No
  external callers passed `completed=`.
- **`hook()` identity is the message.** Two `hook()` registrations sharing a
  message string share one name and one fire ledger — the same semantic
  `nudge`/`gate` have always had. Distinct rules deserve distinct messages.
- **An adversarial review hardened the whole wave before release.** A
  gpt-5.5 finder/refuter pass over the full diff confirmed 29 defects, all
  fixed: the schema migration now serializes concurrent first opens in one
  transaction; `transition()` is compare-and-swap so racing syncs cannot
  overwrite an acceptance; destructive PR-state transitions require a fresh
  gh response (a gh outage or stale cache can never flip `pr_open`);
  `resolved_at` records GitHub's merge time, not sync time, so delayed syncs
  cannot swallow post-merge recurrences; the reopen edge is fix-kind-only
  with an atomic generation bump; the SessionStart announcer fast-fails on
  lock contention and marks a row announced only when its line is actually
  delivered; the plugin's `hooks.json` now registers SessionStart synchronously
  as well (restoring additionalContext SessionStart hooks under 9.0.0), plus
  `review run` on SessionStart for still-open sessions; a per-repo lock
  makes concurrent session ends spawn exactly one brain; triage writes are
  compare-and-set and never override judge-accepted evidence; plan-rejection
  feedback is gated on its extracted text rather than the empty envelope;
  and the steering matrix grew six more pinned sentences.
- **Candidate rows carry lifecycle provenance.** A guarded in-place migration
  adds `generation`, `resolved_at`, `origin_repo_key`, `pack_name`, and
  `announced_status` on first open — historical terminal candidates are
  baselined as already-announced; open PRs deliberately are not, so existing
  unnoticed PRs announce once.
- cc-transcript floor is 11.

## [9.3.0] - 2026-07-12

### Removed
- The `packs.toml` `launcher` key, `read_launcher`, and the `toml_basic_string` helper —
  inert since 9.0.0 went plugin-canonical (the plugin's `hooks.json` fixes the `uvx
  capt-hook` command prefix, so nothing builds hook commands from `launcher`). Its sole
  consumer was pack-manager line preservation across `pack add`/`remove`/`update`.
  Manifest rewrites (`pack add`/`pack remove`/pinned `pack update`) now drop a stale
  `launcher` line on their next write instead of preserving it.

## [9.2.1] - 2026-07-12

### Removed
- The internal `sibling_settings` helper in `captain_hook/cli.py` — dead since `review
  enable` stopped writing settings wiring (9.2.0); nothing read the sibling path anymore.

## [9.2.0] - 2026-07-12

### Changed
- **`review enable` no longer writes a `SessionEnd` hook into `.claude/settings.json`.**
  The plugin's static `hooks.json` (9.0.0) already runs `uvx capt-hook review run` on
  every session end, self-guarded by the watch list, so `enable` now does two things:
  watch the repo and register the plugin. `ensure_review_wiring` and its helpers are
  gone with it. One consequence worth knowing: the session reviewer now rides the
  plugin — a repo where the captain-hook plugin is not enabled gets no reviewer there
  (and no capt-hook hooks at all).

### Fixed
- **A sync and an async dispatch of one event no longer race for a single once-token.**
  The duplicate-dispatch guard keyed only on event name and payload, so the synchronous and
  asynchronous `run <Event>` passes of one event — byte-identical stdin, but disjoint hook
  sets, since `dispatch` filters on `spec.async_` — claimed the same token and one pass was
  silently dropped. The claim key now carries the async variant, so each pass guards
  independently and both run.
- **`SessionEnd` dispatches async pack handlers.** The plugin's `hooks.json` wired
  `SessionEnd` only to `uvx capt-hook review run`, which detaches the session reviewer and
  never reaches `dispatch()`. A second entry, `uvx capt-hook run SessionEnd --async`, now
  runs alongside it, so a pack's `async_=True` `SessionEnd` handler fires fleet-wide.

## [9.1.0] - 2026-07-11

### Changed
- **The model-routing nudges route to gpt-5.6-sol (general pack 0.17.0).** OpenAI's
  gpt-5.6 family replaces gpt-5.5 in the Models-table lanes: the review-routing,
  workflow-routing, implementation-spawn, and inline-edit nudges (messages and LLM
  rubrics) now name gpt-5.6-sol as the codex lane, and their declarative tests assert
  the `gpt-5.6` family substring so a later variant swap doesn't churn them. The
  security-noun prefilters are unchanged.

## [9.0.0] - 2026-07-10

### Changed (BREAKING)
- **The plugin registers every hook event itself.** captain-hook ships a static
  `captain_hook/hooks/hooks.json` that wires all twelve lifecycle events under their
  canonical commands: `uvx capt-hook run <Event>` for the synchronous events, `uvx
  capt-hook run SessionStart --async`, and the always-on `uvx capt-hook review run`
  (async) for `SessionEnd`. Enabling the plugin — which `init` and `skills install`
  already do, via the `extraKnownMarketplaces` + `enabledPlugins` entries in
  `.claude/settings.json` — is now the only wiring a repo needs. Claude Code picks up a
  hook on a brand-new event the next session, with zero settings changes; the
  re-registration step is gone.
- **`init` and the `pack` commands no longer write hook wiring.** `init` still scaffolds
  `.claude/hooks/`, registers the plugin, provisions NLP resources, and arms the session
  reviewer, but it no longer merges a `hooks` block into `.claude/settings.json`. `pack
  add`, `pack remove`, and `pack update` only edit `.claude/hooks/packs.toml`.
- Upgrading: delete any captain-hook `hooks` entries from your committed
  `.claude/settings.json` (and `.claude/settings.local.json`); the plugin registers them
  now. Keep the `extraKnownMarketplaces` + `enabledPlugins` entries — that is what enables
  the plugin. `uvx capt-hook init` or `uvx capt-hook skills install` writes them if they
  are missing.

### Removed (BREAKING)
- **The `register-hooks` command and the settings-merge machinery behind it.** Gone with
  it: `generate_settings`/`merge_settings`, the own/custom/foreign group classifier, the
  `settings.local.json` deferral, and the settings-drift nag that pushed the agent to
  re-run `register-hooks`. Repos no longer merge captain-hook commands into their own
  `.claude/settings.json` — the plugin's `hooks.json` is the single source of the wiring.
  The `launcher` key in `packs.toml` is still round-tripped across pack edits but no
  longer rewrites hook commands; the plugin's canonical `uvx capt-hook` prefix is fixed.
- Coordination: the resident-daemon rollout can no longer ride `register-hooks`. Because
  the plugin's `hooks.json` is static and hardcodes the `uvx capt-hook` prefix, a
  machine-local daemon launcher must use single-string shell dispatch or
  `${CAPT_HOOK_LAUNCHER:-uvx capt-hook}` env indirection inside the plugin `hooks.json`
  command.

### Fixed
- **Duplicate hook dispatch collapses under a once-guard.** Claude Code can spawn several
  byte-identical hook processes for one event; non-decision events now claim a per-event
  once-token so the work runs once. Decision-capable events (`PreToolUse`, `Stop`,
  `SubagentStop`, `PermissionRequest`) stay exempt, so no gate is ever swallowed.
- **Pack cache GC.** Stale content-addressed pack commit directories are pruned after a
  fresh fetch, and pack-GC recency and the once-guard's races are hardened.

## [8.19.0] - 2026-07-10

### Changed
- **Per-event startup went on a diet.** Every `capt-hook run <Event>` paid a
  ~350ms import bill before reading stdin; the bulk was
  `captain_hook/__init__.py` eagerly importing the whole framework. The root
  package now re-exports lazily via PEP 562 (`import captain_hook` dropped from
  ~125ms to ~3ms), heavy dependencies — spawnllm and its backends,
  pydantic-settings, `cc_transcript`'s parser/query/discovery/decisions stacks,
  asyncio — load inside the functions that use them instead of at module
  import, and the inline-test `Input` model is a plain frozen dataclass instead
  of a pydantic one (~90ms to ~14ms for `captain_hook.testing.types`). A plain
  PreToolUse event no longer imports `cc_transcript.parser` at all. The public
  surface is unchanged and now pinned by `tests/test_public_api.py`: the full
  144-name root export set (each name identity-equal to its defining-module
  object), `from captain_hook import *` (the new `__all__` mirrors the
  re-exports), consumer-observed submodule paths, and
  `typing.get_type_hints` introspection over hook specs all behave exactly as
  before.
- **Transcript parsing is deferred to first use.** `HookContext.transcript` is
  a lazy proxy; the full-session read and parse happen only when a condition or
  handler actually touches the transcript. Events that never consult it —
  the highest-frequency shapes — skip the cost entirely: attaching a 27MB
  session transcript to an event now costs ~0ms unless read (previously ~140ms
  on every event, growing with session length). A transcript that fails to
  load mid-dispatch raises `TranscriptLoadError` through the handler boundary,
  so a corrupt or unreadable transcript still fails loud instead of letting a
  blocking hook silently fail open.
- **`Input` keeps pydantic-grade validation.** The dataclass conversion
  re-implements what pydantic enforced: unknown kwargs, wrong-typed fields,
  wrong element/key types in container fields, and `str` transcript paths
  coercing to `Path` (a string path previously loaded zero fixture events
  silently) — each rejection is a `TypeError` naming the field.
- This repo's own hooks launch `"$CLAUDE_PROJECT_DIR"/.venv/bin/capt-hook`
  directly instead of resolving through `uv run` on every event; the
  project-relative launcher recipe is documented in the packs guide.

## [8.18.0] - 2026-07-10

### Fixed
- **Capped hooks no longer over-fire under parallel hook events.** Every hook
  event runs in its own `capt-hook run` process, so a batch of parallel tool
  calls all read the pre-increment fire count and blew past `max_fires` (one
  session delivered 12 warns against a cap of 3). `max_fires` is now
  reserve-then-release: the fire is reserved under a file lock before the
  handler runs, and a handler that declines, raises, or aborts abnormally
  (`SystemExit`/`KeyboardInterrupt`) releases the reservation before the
  exception re-propagates — an abnormal exit no longer leaks the slot and
  permanently mutes the hook. Suppressed and released fires record no ledger
  decision — decision-ledger consumers see only delivered fires, matching the
  capped behavior to date.
- **Signal hooks no longer score the user's words.** `Signals` gained a
  keyword-only `origin` field, an upstream candidate filter orthogonal to
  `scope`. The new default `origin="assistant"` keeps only the agent's own
  prose — assistant messages, thinking blocks, prose-carrying tool calls —
  so a stance nudge no longer fires when the user's message carries the
  trigger vocabulary. **Default flip:** every un-stamped `Signals` bundle
  becomes assistant-only. Stamp `origin="any"` on hooks that legitimately
  score user text; a `UserPromptSubmit` hook that scores the just-submitted
  prompt must stamp it — the prompt joins the scan only under `"any"`, and a
  `window=0` bundle without the stamp has no candidates at all. The general
  pack's distinct-requests nudge and the corrections example are stamped
  accordingly.
- **Relayed and quoted hook output no longer re-triggers signal hooks.**
  `transcript_texts()` drops agent-injected user events — teammate-message
  relay banners, scheduled-task injections, role reminders — under either
  origin, via cc-transcript's `UserEvent.is_agent_injected`. A session-wide
  verbatim echo ledger (`PrimitiveState.echo_verbatim`), seeded from each fired
  warning's sentences and deduplicated so a warning that repeats one sentence
  cannot flood and evict the ledger, damps text that quotes a fired warning:
  the seeded sentences are stripped from the candidate and only the remainder
  is scored, so a pure quote is damped while a quote that also carries a fresh
  violation still fires on the remainder. Damping is whitespace-normalized,
  shared across hooks, and independent of the lemma echo window, whose forward
  horizon now also covers the bundle's lookback span. Requires cc-transcript
  >= 10.6.
- **Signal consumption is the authoritative fire claim.** A signal-scored LLM
  hook read its candidates lock-free before the verdict, so two concurrent
  hook processes could both clear the pre-gate on one signal and both deliver.
  Consumption now re-matches under the state lock after the verdict, through
  the same candidate filter the pre-gate uses; an empty locked re-match — the
  signal already consumed by a peer, or absorbed by a quote or veto — aborts
  the fire rather than delivering a signal it never claimed.

### Added
- **`SessionSlot.mutate()`.** Transactional get→edit→set on session state
  under a sibling file lock, ported up from `DurableSlot` (which now inherits
  it). Every whole-model session-state write — fire counts, consumed ledgers,
  echo state — routes through it, closing the lost-update race between
  concurrent hook processes. `WorkflowState` gained an opt-in `mutate(evt)`
  classmethod.

### Changed
- **cc-transcript 10.6.** Pin bumped to `>=10.6,<11` for
  `UserEvent.is_agent_injected` (the relay-banner marker, now start-anchored so
  a mid-message mention of a banner tag no longer reads as an injection) and the
  turn-segmentation fix that keeps a relay banner from opening a fake turn.

## [8.17.0] - 2026-07-10

### Changed
- **`Waiting()` consults the shared cc-transcript activity oracle.** `is_waiting` keeps its Stop-payload short-circuit (`background_tasks`/`session_crons`) and no-transcript guard, then delegates every transcript judgment to `probe_events` over the already-loaded transcript — the same oracle cc-vigil consumes, replacing the local `ephemeral_wait`/`pending_async` reimplementation. The oracle brings delivery-aware completion (a pending async task clears only when its notification is delivered or drained), compaction-safe turn boundaries, and alias-/MCP-aware tool-name matching: an `Execute` background command counts as backgrounded Bash, and `mcp__<server>__SendMessage` matches a configured `SendMessage`. Requires cc-transcript >= 10.4.

## [8.16.0] - 2026-07-10

### Changed
- **`max_fires` budgets count per agent context.** One shared session ledger
  meant a single subagent's fire consumed the whole session's `max_fires`
  budget — every other agent (and the orchestrator) then saw nothing, however
  many violations followed. The `HookState` ledger now lives at
  `sessions/<sid>/<hook>/<agent_id|main>/`, so the main agent and each
  subagent spend their own allowance; a missing, null, or empty `agent_id`
  all mean the main agent. Session-scoped `PrimitiveState` (the turn throttle
  and echo window) stays global by design. Note: a `SubagentStop` guard with
  `max_fires=1` now fires once per subagent rather than once per session.

### Added
- **`BaseHookEvent.agent_id`.** The subagent id attached to the event, `None`
  for main-agent events; `is_subagent` is now derived from it.

### Fixed
- **cc-context MCP edits now hit `Tool("Edit"/"Write")` conditions.** Via
  cc-transcript ≥10.3, `mcp__cc-context__ccx_code_edit` / `ccx_code_replace`
  alias to `Edit` / `Write` in tool-name matching, so name-gated hooks no
  longer miss edits routed through the cc-context MCP. Payload-shape
  conditions (e.g. diff-gated comment budgets) still see an untyped call —
  typed lowering is tracked upstream.

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
