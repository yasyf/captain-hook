# The `capt-hook review` CLI

The reviewer's command surface, run as `uvx --isolated capt-hook review <command>`. The store
lives outside the repo (under capt-hook's state dir), so every command sees the same
candidates regardless of cwd; commands taking `--repo` default to the repo containing
the current directory.

Scan-time routing — the `routing:` line and the pack attribution columns — applies to
fix candidates only. A misfire of a *pack* hook opens its fix PR against the pack's
own repo (a builtin pack
routes to captain-hook; a declared external pack routes to `github.com/<owner>/<repo>`),
so such a candidate's `repo_key` is the pack's repo while its `origin_repo_key` records
the watched repo the misfire fired in. A create candidate's row always carries
`repo_key` = the watched repo, no `pack_name`, and no routing line — but its PR may
still open against a pack's repo when the brain's overlap check (SKILL Step 3)
upgrades it to an edit of a pack hook; that decision is made at implementation time
and is never stored on the row. Every `--repo <key>` filter matches a candidate
whose `repo_key` **or** `origin_repo_key` is `<key>`, so a rerouted pack fix stays visible
from the repo it fired in, and its eligibility is gated on *that* origin repo's watching
flag — the pack's repo need not itself be watched.

Candidate statuses: `watching → pr_open → {stale, accepted, rejected}`, plus a direct
`watching → rejected` edge and an `accepted → watching` reopen edge. The `watching →
rejected` edge is the judge retiring a candidate whose evidence it rejected, so
`rejected` no longer implies a closed PR — a judge-retired candidate never had one.
`stale` can still move to `accepted`/`rejected`; `rejected` is terminal. An `accepted`
fix candidate reopens to `watching` — its `generation` counter bumped and its `pr_url`
(the prior, insufficient fix) kept — when a fresh judge-confirmed misfire lands after
the fix merged; a reopened candidate then counts only evidence newer than the merge, so
one strong recurrence re-qualifies it. Illegal moves fail with an error.

## Commands

### `review run`

The SessionEnd and SessionStart hook the captain-hook plugin registers, async
(fire-and-forget). Reads the hook payload from stdin, guards, and detaches the reviewer
child; always exits 0, and skips non-interactive `claude -p` session ends. The plugin
wires it to SessionStart as well, so the next session start in a repo sweeps any
still-open sibling session left running overnight. A repo with no watching record is
enrolled as watched on first sight; an explicit `disable` sticks. Claude Code calls
this — you never do.

### `review spawn --transcript <path> [--cwd <dir>]` (hidden)

The detached reviewer pass over one ended session: scan, judge, PR sync, and — when
candidates are eligible — spawning this skill. `--transcript` is required; `--cwd`
defaults to the process cwd. This is what spawned you; do not recurse into it.

### `review enable` / `review disable`

`enable` marks the current repo watched and registers the captain-hook plugin
marketplace. Running it by hand is rarely needed: `review run` enrolls any repo it
first sees, so a plugin-wired repo is watched from its first session end.
`disable` is the durable opt-out — it sticks across future `review run` passes;
candidates stay recorded but never become eligible.

### `review scan [--transcript <file>]... [--dir <dir>]...`

Incrementally scans explicit transcript files (and directories searched recursively for
`*.jsonl`) for user corrections. At least one `--transcript` or `--dir` is required.
Prints `scanned N transcripts, M new corrections`. Re-scanning an unchanged file is a
no-op.

### `review triage [--limit N]`

Judges stored corrections lacking an LLM verdict at their taxonomy's current prompt
version (manual/backfill path; the detached child already runs this per session).
The CREATE and FIX prompts version independently, so bumping one never re-judges the
other lane. `--limit` overrides the per-session call cap. Prints one report line,
then one line per near-duplicate slug pair the pass surfaced:

```
judged N, failed N, pending N, merged M, retired R, reopened R
possible split: <slug-a> ~ <slug-b> (0.93)
```

`merged` counts observations the closing regroup re-parented onto their canonical slug
candidate; `retired` counts watching create candidates it rejected (every observation
judged, none accepted); `reopened` counts accepted fix candidates the pass returned to
`watching` because a judge-confirmed misfire recurred after their fix merged. Failed rows
stay pending and retry next pass. Each `possible
split` line names two canonical slugs whose evidence nearly coincides — the judge may
have minted two names for one rule — with their cosine similarity; nothing merges
automatically.

### `review status [--repo <key>] [--no-sync]`

The rich human dashboard (also `capt-hook status`): the funnel of tracked candidates by
lifecycle stage, topped by a reviewer-health line. When the detached reviewer is
healthy that line carries a judge segment — `judge: N pending · last verdict <age>`,
where `N` is the judge-worthy backlog at each lane's current prompt version — and, when the
pass surfaced any, an `S possible slug splits` count. `--no-sync` skips the background
`gh` refresh of open PRs.

### `review list [--repo <key>]`

One line per candidate, newest first:

```
#12 [watching] create/transcript_message x3: never force-push to main, use --force-with-lease
#31 [watching] fix/hook_complaint x2 -> github.com/yasyf/captain-hook: the docs nudge re-fired…
```

— id, status, `candidate_kind/source_kind`, observation count, and the first 80 chars
of the earliest observation's verbatim text. A cross-repo pack fix (one whose PR targets a
different repo than the one it fired in) carries a ` -> <repo_key>` suffix after the count,
naming the repo its PR opens against. The suffix reflects scan-time routing only — a
create candidate the brain routes at a pack via the overlap check never shows one.

### `review show <ID>`

Every column of one candidate's row (`repo_key`, `candidate_kind`, `rule`,
`source_kind`, `status`, `pr_url`, `pr_opened_at`, `generation`, `resolved_at`,
`sample_text`, `observations`, ...) plus its threshold line:

```
thresholds: sessions=3 days=2 open_prs=0 single_observation=False eligible=True
```

`rule` is the candidate's grouping key, and it never upgrades in place. A scan keys every
new candidate by a content digest. At the close of a judge pass, the regroup re-parents
each judge-accepted observation onto a slug-keyed candidate (minted on first need) and
sweeps the emptied digest candidate; a candidate whose observations are all judged with
none accepted retires with its digest key, and a mixed or still-unjudged candidate stays
watching with its digest key. A slug-keyed `rule` therefore always matches `SLUG_PATTERN` —
two to six hyphenated `[a-z0-9]` groups — while a 64-char content digest never does.

Fix candidates (`candidate_kind=fix`, `source_kind=hook_complaint`) additionally carry
`target_source_file` (the hook file to amend), `target_hook_name` (its registered
name), and `misfire_class` (e.g. `refire`, `false_positive`); `sample_text` is
Claude's verbatim complaint. Fix thresholds are looser: `min_sessions_fix` distinct
sessions, or one observation that is both judge-accepted and heuristically VERY_HIGH
(`single_observation=True`).

A pack-hook fix carries `origin_repo_key` (the watched repo the misfire fired in) and
`pack_name` (the pack), and its `repo_key` is the pack's own repo. For such a candidate
`show` prints an extra routing line — the repo its PR opens against, the pack, and the
origin repo — so you clone the right repo before drafting the fix:

```
routing: target_repo=github.com/yasyf/captain-hook pack=general origin_repo=github.com/yasyf/scratch
```

A create candidate never prints a `routing:` line — its target is the brain's
implementation-time decision (SKILL Step 3), not a stored column.

### `review threshold-check [ID] [--repo <key>]`

The eligibility verdict — the source of truth. Without `ID`, reports every candidate in
the repo:

```
#12 eligible=True sessions=3/3 days=2/2 open_prs=0/2 watching=True
```

Counts are **judge-accepted** observations only (distinct sessions, distinct UTC days);
unjudged observations count as not-yet. `eligible=True` already accounts for the
watching flag and the repo-wide open-PR cap — never re-derive any of this. `open_prs`
counts live open PRs by the repo each PR targets, parsed from the PR's URL.

### `review slots [--repo <key>]`

The open-PR cap check. Prints one line — `<repo>: open_prs=<n>/<max> free=<free>` —
counting live open PRs by the repo each PR targets (parsed from the PR's URL), and
exits 1 when `free=0`. The brain runs it immediately before `gh pr create`, passing
whatever repo the PR targets — for any pack-targeted PR, routed fix or
create-as-edit alike, that is the pack repo's key. A full target repo is a logged
skip, not an error.

### `review update <ID> <status> [--pr-url <url>] [--pr-title <title>]`

Moves a candidate along the lifecycle. The one you use:

```bash
uvx --isolated capt-hook review update 12 pr_open \
  --pr-url https://github.com/owner/repo/pull/7 --pr-title "[capt-hook] Block force-pushes"
```

`--pr-url` stamps the URL and `pr_opened_at` onto the candidate; `--pr-title` stamps the
PR title, which the desktop notification the move fires reads (a `pr_open` or `accepted`
move sends one). Statuses: `watching`, `pr_open`, `stale`, `accepted`, `rejected` — but
only moves allowed by the lifecycle succeed, and merge/close outcomes are `sync-prs`'s
job, not yours. `--pr-title` is additive: an older cached CLI rejects it, so retry without
it on `no such option`.

### `review sync-prs [--repo <key>]`

Folds each open PR's GitHub state back into its candidate via `gh pr view`: merged →
`accepted`, closed → `rejected`, open past the stale window → `stale` (freeing its slot
under the open-PR cap). Prints the transition counts. The detached child runs this each
pass; run it manually only when reconciling by hand.

## Companion commands

### `capt-hook hooks`

Not under `review` — the top-level active-hook inventory, run as
`uvx --isolated capt-hook hooks`. The overlap check's input (SKILL Step 3): one
tab-separated line per hook discovered in the current repo, six columns, sorted by
pack, source file, then hook name. Always exits 0; zero hooks prints nothing. No
tests execute; discovery resolves packs exactly as `capt-hook test` does, so a
builtin-only repo stays offline (a stale external pack may refresh).
Framework-internal registrations — the PR announcer, NLP provisioning — are
omitted; every row is a hook some repo or pack owns.

```
fixes	github.com/yasyf/captain-hook	permissions.py	fixes.scratch_writes:approve_scratch_dir_writes_under_skip_permissions	PreToolUse|PermissionRequest
general	github.com/yasyf/captain-hook	commands.py	general.commands:hook_8aef6be4	PreToolUse	BLOCKED: git stash is not allowed…
local	-	nudge.py	hooks.style:gate_aa3a1d5b	Stop|SubagentStop
```

- **pack** — the pack the hook ships in, or `local` for a repo-local
  `.claude/hooks/` hook.
- **home repo** — the repo key a pack edit targets: a builtin pack prints
  captain-hook's key, a cached external pack its declared repo; `-` for repo-local
  hooks and for packs the pack index cannot resolve offline.
- **source file** — the hook file's basename.
- **hook name** — the registered hook name, as the fire log and `review show`'s
  `target_hook_name` spell it.
- **events** — pipe-joined event names.
- **message first line** — the hook's message when it is a plain string, else the
  handler docstring's first line, else empty. An LLM hook prints its docstring or
  nothing — its message lives in the judge closure, never on the spec — so screen
  LLM hooks by name and source file instead.

Its output is a parsed contract, same as the `review` commands — format changes stay
additive.
