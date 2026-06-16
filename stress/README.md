# Stress harness — SessionEnd reviewer

A live, re-runnable stress test for captain-hook's SessionEnd reviewer pipeline
(`capt-hook review`): the path that mines ended Claude Code sessions for durable
corrections and confirmed hook misfires, judges them with an LLM, and — once a
candidate crosses its thresholds — spawns a headless brain that opens a hook PR.

This harness is **dev-only**. It lives outside `testpaths` so pytest never
collects it, and outside the packaged module so it never ships (a CI guard in
`.github/workflows/ci.yml` asserts `stress/` is absent from the wheel and sdist).

## What it proves

For correctness, 68 offline scenarios across 13 families assert exact pipeline
behavior against hand-planted transcripts: DB rows, candidate statuses, exit
codes, spawn.log lines, SQLite integrity. Every check carries raw evidence.

For efficacy, the live tier runs the real Sonnet judge against a frozen golden
set and hand-labeled corrections, and the brain tier drives real `claude -p`
sessions through a throwaway GitHub repo to a real hook PR, then folds a merge
back to `accepted`.

## Prerequisites

- `uv` with the project synced (`uv sync --extra dev`).
- For the **judge** tier: the `claude` CLI logged in (the judge shells out to it).
- For the **brain** tier: `claude` logged in and `gh` authenticated with the
  `delete_repo` scope (`gh auth refresh -h github.com -s delete_repo`). The brain
  tier creates a private `capt-hook-stress-<ts>` repo under your account.
- Always invoke through `uv run` so the harness pins the local checkout, not the
  PyPI wheel:

```bash
env -u UV_EXCLUDE_NEWER uv run --no-sync python -m stress.cli <command>
```

(`UV_EXCLUDE_NEWER` is stripped because a global value breaks cc-transcript 2.x
resolution.)

## Running

```bash
# list every scenario by tier
python -m stress.cli list

# offline only — deterministic, free, ~2 min, 68 scenarios
python -m stress.cli run --live none

# + live Sonnet judge (golden gate >= 12/14, label accuracy) — ~$1-2
python -m stress.cli run --live judge

# + live brain end-to-end (real sessions -> real PR) — ~$4, ~20 min
python -m stress.cli run --live brain

# narrow to a family or scenario substring
python -m stress.cli run --live none --only crash
```

Tiers are inclusive and ordered cheapest-first: `judge` runs everything `none`
runs plus the judge scenarios; `brain` adds the end-to-end leg. Each scenario
runs in its own throwaway sandbox (a temp state dir, git repo, and PATH of
shims) that is destroyed after the scenario unless you pass `--keep-sandbox`.
Reports land in `stress/reports/<utc-ts>-<tier>.md` (gitignored).

## Safety

- Every sandbox roots its state under `CAPTAIN_HOOK_STATE_DIR` and
  `CAPT_HOOK_DECISIONS_DB` inside a `/tmp/capt-stress/<run>/` dir; `sandbox.env()`
  drops `CAPT_HOOK_SPAWNED`, `CLAUDE_PROJECT_DIR`, and `UV_EXCLUDE_NEWER` so a
  command can neither escape to the real `~/.claude/state` nor self-skip.
- After every scenario the runner queries the real review DB and decision ledger
  for sandbox fingerprints (`capt-hook-stress` repo keys, `/tmp/capt-stress`
  paths, `stress-` session ids) and aborts the whole run on any hit.
- **macOS Tahoe (26.x):** every `exec` of an `adhoc, linker-signed` Mach-O makes
  `syspolicyd` re-walk the trust anchors, and uv-managed CPython ships that
  signature. The suite execs it on every `capt-hook` call and detached child, so
  before any scenario `stress.cli run` calls `signing.ensure_stable_signatures()`,
  which re-signs the interpreter once with a stable ad-hoc identity
  (`codesign --force --sign -`); `syspolicyd` then caches the assessment and the
  spawns cost nothing. The call is idempotent and a no-op off Darwin. The harness
  also uses no compiled binary fixtures (the torn-mtime crash test reproduces with
  data volume) and keeps spawn counts bounded. Destroy stray sandboxes with
  `python -m stress.cli clean`. If `syspolicyd` is still pinned from a pre-fix
  run, `sudo killall syspolicyd` lets launchd respawn it clean.

## Cleanup

```bash
python -m stress.cli clean                 # remove leftover /tmp/capt-stress run dirs
python -m stress.cli nuke-github --i-know  # close PRs on and delete every capt-hook-stress-* repo
```

## Layout

```
stress/
├── cli.py            # the runner: tiers, phase ordering, real-state guard, clean/nuke-github
├── sandbox.py        # Sandbox: isolated state dir + git repo + shim PATH; leak guard
├── shims.py          # capt-hook/uvx pinned to the checkout; deterministic claude + gh stubs
├── corpus.py         # labeled synthetic transcripts (reuses tests/ builders) + pathology generators
├── seeds.py          # decision-ledger seeding + offline verdict injection (mirrors the judge)
├── db.py             # read-only SQLite assertions (mode=ro, never disturbs WAL)
├── report.py         # markdown report: scenario table, findings ledger, evidence
├── drivers/
│   ├── proc.py       # capt-hook invoker, SpawnReport parser, wait_drained/kill_when/hammer
│   ├── claude_live.py# real claude -p sessions; SessionEnd-payload transcript capture
│   └── github.py     # throwaway private repo lifecycle (create, rewire, merge, delete)
└── scenarios/        # one module per family; each exposes scenarios() -> tuple[Scenario, ...]
```

## Adding a scenario

Write `run_<name>(sandbox) -> ScenarioResult` returning `check(...)` calls that
carry raw evidence, append a `Scenario(...)` to the module's `scenarios()`
tuple, and the registry in `scenarios/__init__.py` picks it up. Drive the
pipeline with `stress.drivers.proc` helpers (`review_run`, `wait_drained`,
`spawn_reports`) and assert with `stress.db.query`. A scenario that uncovers a
real pipeline behavior must **assert the observed behavior** (so the suite stays
green and regression-pinned) and set `ScenarioResult(finding="...")` — never
contort the scenario to hide it.

## Findings ledger

Behaviors the harness pins as observed (green, regression-locked) rather than
bugs it works around:

- **torn-mtime-commit** — `scan.py` `ingest()` commits the file's mtime row and
  every feedback event in one transaction *before* the per-statement-autocommit
  candidate/observation loop. A SIGKILL between them leaves observations torn,
  and because the mtime gate then reports `scanned=0` on every rescan of the
  unchanged file, the lost observations are never backfilled.
- **decisions-db WAL-open race** — `cc_transcript/decisions.py` runs
  `PRAGMA journal_mode = WAL` before `PRAGMA busy_timeout`, so two reviewer
  children opening a fresh ledger concurrently can crash one with
  `database is locked` (~40% of paired deliveries); the crash precedes
  `record_file_scan`, so that session is scanned away silently.
- **same-mtime / truncated-mid-line silent miss** — mtime is the only change
  detector. A transcript rewritten in place with its mtime restored, or a
  truncated prefix later completed without bumping mtime, is never re-ingested.
- **reviewer self-skip is first-message only** — `is_reviewer_session` tests only
  the first user message, so a reviewer marker arriving mid-conversation does not
  self-skip and the marker turn itself becomes a feedback event.
- **multi-session file satisfies min_sessions** — eligibility counts distinct
  session ids, not files, so a single resumed/concatenated transcript carrying
  several session ids can cross `min_sessions` alone.
- **tier switch re-judges the corpus** — verdicts key on the resolved model
  string, so flipping `HOOKS_REVIEW_JUDGE_TIER` re-runs one LLM call per stored
  row instead of reusing prior verdicts.
- **wired command resolves uvx, not the checkout** — `review enable` writes
  `uvx capt-hook review run`, so real session ends resolve the PyPI wheel; the
  brain driver must rewire it (the harness shims `uvx` to intercept).
