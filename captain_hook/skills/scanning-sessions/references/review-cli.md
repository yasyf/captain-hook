# The `capt-hook review` CLI

The reviewer's command surface, run as `uvx capt-hook review <command>`. The store
lives outside the repo (under capt-hook's state dir), so every command sees the same
candidates regardless of cwd; commands taking `--repo` default to the repo containing
the current directory.

Candidate statuses: `watching → pr_open → {stale, accepted, rejected}` (`stale` can
still move to `accepted`/`rejected`; `accepted`/`rejected` are terminal). Illegal moves
fail with an error — there is no transition back to `watching`.

## Commands

### `review run`

The wired SessionEnd hook entry. Reads the hook payload from stdin, guards, and
detaches the reviewer child; always exits 0. Claude Code calls this — you never do.

### `review spawn --transcript <path> [--cwd <dir>]` (hidden)

The detached reviewer pass over one ended session: scan, judge, PR sync, and — when
candidates are eligible — spawning this skill. `--transcript` is required; `--cwd`
defaults to the process cwd. This is what spawned you; do not recurse into it.

### `review enable` / `review disable`

`enable` marks the current repo watched and wires the SessionEnd hook into
`.claude/settings.local.json` (idempotent). `disable` stops watching; candidates stay
recorded but never become eligible.

### `review scan [--transcript <file>]... [--dir <dir>]...`

Incrementally scans explicit transcript files (and directories searched recursively for
`*.jsonl`) for user corrections. At least one `--transcript` or `--dir` is required.
Prints `scanned N transcripts, M new corrections`. Re-scanning an unchanged file is a
no-op.

### `review triage [--limit N]`

Judges stored corrections lacking an LLM verdict at the current prompt version
(manual/backfill path; the detached child already runs this per session). `--limit`
overrides the per-session call cap. Prints `judged N, failed N, pending N`; failed rows
stay pending and retry next pass.

### `review list [--repo <key>]`

One line per candidate, newest first:

```
#12 [watching] create/transcript_message x3: never force-push to main, use --force-with-lease
```

— id, status, `candidate_kind/source_kind`, observation count, and the first 80 chars
of the earliest observation's verbatim text.

### `review show <ID>`

Every column of one candidate's row (`repo_key`, `candidate_kind`, `rule`,
`source_kind`, `status`, `pr_url`, `pr_opened_at`, `sample_text`, `observations`, ...)
plus its threshold line:

```
thresholds: sessions=3 days=2 open_prs=0 single_observation=False eligible=True
```

Fix candidates (`candidate_kind=fix`, `source_kind=hook_complaint`) additionally carry
`target_source_file` (the hook file to amend), `target_hook_name` (its registered
name), and `misfire_class` (e.g. `refire`, `false_positive`); `sample_text` is
Claude's verbatim complaint. Fix thresholds are looser: `min_sessions_fix` distinct
sessions, or one observation that is both judge-accepted and heuristically VERY_HIGH
(`single_observation=True`).

### `review threshold-check [ID] [--repo <key>]`

The eligibility verdict — the source of truth. Without `ID`, reports every candidate in
the repo:

```
#12 eligible=True sessions=3/3 days=2/2 open_prs=0/2 watching=True
```

Counts are **judge-accepted** observations only (distinct sessions, distinct UTC days);
unjudged observations count as not-yet. `eligible=True` already accounts for the
watching flag and the repo-wide open-PR cap — never re-derive any of this.

### `review update <ID> <status> [--pr-url <url>]`

Moves a candidate along the lifecycle. The one you use:

```bash
uvx capt-hook review update 12 pr_open --pr-url https://github.com/owner/repo/pull/7
```

`--pr-url` stamps the URL and `pr_opened_at` onto the candidate. Statuses: `watching`,
`pr_open`, `stale`, `accepted`, `rejected` — but only moves allowed by the lifecycle
succeed, and merge/close outcomes are `sync-prs`'s job, not yours.

### `review sync-prs [--repo <key>]`

Folds each open PR's GitHub state back into its candidate via `gh pr view`: merged →
`accepted`, closed → `rejected`, open past the stale window → `stale` (freeing its slot
under the open-PR cap). Prints the transition counts. The detached child runs this each
pass; run it manually only when reconciling by hand.
