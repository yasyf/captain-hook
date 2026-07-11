---
name: scanning-sessions
description: The headless session-reviewer brain — turns a watched repo's PR-eligible candidates into pull requests, both kinds. Invoked as /captain-hook:scanning-sessions --transcript <path> inside the target repo by capt-hook's detached SessionEnd reviewer pipeline. Enumerates judge-accepted, threshold-eligible candidates via uvx capt-hook review (the CLI is the source of truth), re-verifies every cited correction or complaint verbatim against its session transcript, then per candidate drafts a new hook (create candidates) or amends the attributed misfiring hook with a regression test (fix candidates) in a worktree by delegating to the authoring-hooks skill, proves it with uvx capt-hook test, opens exactly one PR with the verbatim evidence, and records the PR on the candidate. Use when a prompt starts with /captain-hook:scanning-sessions, or to review eligible capt-hook candidates and open hook PRs.
argument-hint: "--transcript <path to the ended session's transcript>"
allowed-tools: Read, Grep, Glob, Bash, Skill
---

# Scanning Sessions into Hook PRs

You are the session reviewer's brain. capt-hook's detached SessionEnd pipeline spawned
you inside a watched repo because at least one candidate crossed its PR thresholds.
Candidates come in two kinds — `review show <ID>` prints `candidate_kind`:

- **create** — users corrected Claude; the PR adds a **new** `.claude/hooks/<slug>.py`.
- **fix** — Claude itself complained that an existing hook misfired, and the complaint
  was attributed to that hook via the fire log; the PR **amends** the hook named by
  `target_source_file`/`target_hook_name` and adds a regression test reproducing the
  misfire.

Your job: for each eligible candidate, draft the hook (or the fix) it implies, prove
it, and open **one** pull request carrying the verbatim evidence.

The prompt that invoked you carries a `[capt-hook-session-reviewer]` marker line after
the command. That marker is how the reviewer's scanner recognizes (and skips) its own
sessions — **any sub-session prompt you spawn must include the
`[capt-hook-session-reviewer]` marker line too**, or your own activity gets mined as
user feedback next pass.

## Hard Rules

- **The CLI is the source of truth.** `uvx capt-hook review threshold-check` / `show`
  decide eligibility — judge acceptance, session/day thresholds, the open-PR cap, the
  watching flag. Never re-derive thresholds yourself; never PR a candidate the CLI
  calls ineligible. Full command surface: [review CLI](references/review-cli.md).
- **One PR per candidate.** Never batch candidates into one PR; the open-PR cap is
  already enforced by the CLI's eligibility call.
- **Re-verify every quote before acting.** Each candidate's verbatim correction must
  appear in a real session transcript; an unverifiable candidate is skipped, never
  PR'd.
- **All writes happen in a worktree or temp-dir clone off the target repo's
  `origin/<default>`** — never commit on the user's checkout. Procedure:
  [PR workflow](references/pr-workflow.md).
- **The PR lands where the hook lives — three-way.** A hook in the watched repo's
  `.claude/hooks/` (and a repo-specific create) is fixed there, in a worktree, as
  today. A **builtin-pack** hook — `show`'s `routing:` line targets captain-hook with
  `pack=` set — is fixed in a captain-hook clone (`gh repo clone`, temp dir) under
  `captain_hook/packs/<pack>/`, verified with `uv run --project . capt-hook test`,
  and PR'd against captain-hook. An **external-pack** hook is fixed in a clone of the
  pack's own repo, verified with `uvx capt-hook --hooks <dir> test`, and PR'd there.
  A denied push to the target repo is a **logged skip** — never fall back to
  committing the fix in the watched repo; a pack hook patched locally diverges from
  the pack and re-breaks on its next update.
- **Fix candidates: re-verify the target hook still exists at `origin/<default>` HEAD
  before drafting.** `git cat-file -e "origin/$default:<target_source_file>"` must
  succeed AND the file must still register the hook (`rg` for its condition or
  message). For a pack fix, run both checks in the target repo's clone — the file
  lives there, not in the watched repo. A vanished, moved, or otherwise
  unattributable target is **skipped** — the candidate stays `watching`, and you
  never open a PR against a hook that is no longer there.
- **You do not draft hooks yourself** — this skill carries no Write/Edit. Drafting
  happens inside the `captain-hook:authoring-hooks` skill, invoked via the Skill tool.
- **`uvx capt-hook test` must be green** in the worktree before `gh pr create`.
- **Run to completion — never stop early.** You run headless; a text-only reply ends
  the session immediately. After the authoring-hooks skill returns, keep going in the
  same run: the job is done only when every eligible candidate either has
  `uvx capt-hook review update <id> pr_open --pr-url <url>` recorded for a created PR
  or has been explicitly skipped with a logged reason. Summaries come last, after
  Step 7 — never between steps.
- **Stay inside the workflow.** Never edit the user's checkout or any
  `.claude/settings*.json` (settings edits stall headless runs on a permission
  prompt you cannot answer), never re-wire hooks, and never chase problems you
  notice along the way — a failed check means *log the skip and move on to the
  final report*, not improvise a fix.

## Workflow

Copy this checklist into your response and check off steps as you complete them:

```
Review Progress:
- [ ] Step 1: Enumerate eligible candidates (threshold-check / list / show)
- [ ] Step 2: Re-verify each candidate's quotes against transcripts
       (fix candidates: also re-verify the target hook at origin/<default> HEAD)
- [ ] Per eligible, verified candidate:
  - [ ] Step 3: Pick the target repo (create candidates: classify repo-specific vs
         generic), then worktree off origin/<default> — or a temp-dir clone for a
         cross-repo target
  - [ ] Step 4: Draft via the authoring-hooks skill (fix candidates: FIX mode —
         amend the target hook + regression test)
  - [ ] Step 5: Verify (uvx capt-hook test green in the worktree)
  - [ ] Step 6: Commit, push, gh pr create (verbatim evidence in the body)
  - [ ] Step 7: review update <ID> pr_open --pr-url <url>
- [ ] Step 8: Final report
```

### 1. Enumerate eligible candidates

```bash
uvx capt-hook review threshold-check
uvx capt-hook review list
uvx capt-hook review show <ID>      # for each candidate threshold-check marks eligible=True
```

`threshold-check` prints one line per candidate with `eligible=`, the judge-accepted
session/day counts against their thresholds, the open-PR count against its cap, and the
watching flag. Work only the `eligible=True` candidates; everything else stays
untouched. `show <ID>` adds the full row — `rule`, `source_kind`, `sample_text` (the
earliest observation's verbatim correction), and the observation count.

### 2. Re-verify the quotes

For each eligible candidate, take the verbatim correction text from `review show` and
confirm it appears, verbatim, in a session transcript before acting:

```bash
rg -F "<the exact correction text>" <the --transcript path> ~/.claude/projects/<munged-cwd>/*.jsonl
```

(`<munged-cwd>` is the repo's absolute path with `/` replaced by `-`.) Transcripts are
JSONL with newlines escaped, so verify a multi-line correction line by line. A candidate
whose correction cannot be found verbatim in any transcript does **not** get a PR —
skip it; it stays `watching` and the next session's scan re-evaluates it. Record the
skip and its reason for the final report. For verified candidates, note which transcript
each quote was found in: the JSONL filename stem is the session id and the matching
line's `timestamp` is the date — the PR body's Evidence section cites both.

For **fix** candidates the quote is Claude's own complaint (an assistant turn), and
two extra checks gate the draft:

1. the target hook re-verification from Hard Rules — exists at `origin/<default>`
   HEAD and still registers the hook;
2. the attribution is still coherent — `target_source_file` and `target_hook_name`
   from `review show` name one concrete hook registration.

Failing either check → skip (stays `watching`), never PR.

### Where a create candidate's hook lands

Before Step 3, classify each verified **create** candidate — fix candidates are
already routed (a pack fix's `routing:` line names its target repo):

- **Repo-specific** — the rule names this repo's paths, commands, tools, or
  vocabulary ("run `uv sync --extra dev` after dependency changes", "docs pages live
  in `docs/*.qmd`"). It lands as a new `.claude/hooks/<slug>.py` in the watched repo;
  this is the default.
- **Generic behavioral** — the rule carries no repo-specific paths, tools, or
  vocabulary and plausibly applies to any repo. It lands as a PR amending or adding a
  hook under `captain_hook/packs/general/` in a captain-hook clone. "Wait for plan approval
  before implementing" is the canonical shape: no path, no command, no project noun —
  every repo with plan mode has this rule, and it was once wrongly PR'd as a
  repo-local hook. A `seen_in_repos: N` line on `review show` — the same slug tracked
  under more than one repo — is strong evidence for generic.
- **When uncertain, repo-local.** A rule that sounds generic but needs the repo's own
  command to check it ("run the tests before stopping" — whose test command?) stays
  repo-local. A wrong general-pack PR ships the rule to every pack user; a wrong
  repo-local hook inconveniences one repo.

### 3-7. One PR per candidate

Follow [references/pr-workflow.md](references/pr-workflow.md) exactly:

1. **Target + tree** — a repo-local target gets a worktree: fetch, then
   `git worktree add` a `capt-hook/review/<rule-slug>` branch off `origin/<default>`.
   A cross-repo target — any fix with a `routing:` line, or a create classified as
   generic — gets a temp-dir clone of the target repo on branch
   `capt-hook/review/pack-<slug>` instead, per pr-workflow's
   "Cross-repo (pack) fix" section.
2. **Draft** — invoke the `captain-hook:authoring-hooks` skill via the Skill tool, passing the
   verbatim correction, its context, and the worktree (or clone) path. It picks the
   primitive, writes `.claude/hooks/<slug>.py` with inline tests (one firing on the
   offending shape, one `Allow()` on a benign neighbor), and runs `uvx capt-hook
   test`; a generic create instead amends or adds the hook under
   `captain_hook/packs/general/` in the captain-hook clone. For a **fix** candidate,
   invoke its FIX mode instead, passing the target hook file, the misfire class, and
   the verbatim complaint — it amends the hook and adds the mandatory regression test
   (silent on the misfiring input, still firing on the genuine case).
3. **Verify** — run `uvx capt-hook test` in the worktree yourself (in a clone, the
   pack-kind verify command from the Hard Rules); green or the candidate is skipped
   this pass.
4. **PR** — commit the hook file, push the branch, `gh pr create` with the template
   body: the rule, the hook's behavior, and an Evidence section quoting each verbatim
   correction with its session id and date.
5. **Record** — `uvx capt-hook review update <ID> pr_open --pr-url <url>`, then remove
   the worktree. If that update fails with `no candidate with id <ID>` or a transition
   error like `rejected -> pr_open`, a concurrent judge pass regrouped the candidate
   (realistic when sessions share one review database) — **do not abort the run**.
   Re-run `uvx capt-hook review list --repo <key>` and stamp the successor create
   candidate whose `rule` equals the branch's slug; if none exists (judge-retired), note
   the already-opened PR in the final report and continue.

### 8. Final report

One row per eligible candidate:

```
| # | rule | action | pr | reason |
|---|------|--------|----|--------|
| 12 | logger-not-print | PR opened | <url> | 3 sessions / 2 days judge-accepted |
| 14 | vague-preference | skipped | — | quote not found verbatim in transcripts |
```

## References

- [review CLI](references/review-cli.md) — the real `capt-hook review` command surface and flags.
- [PR workflow](references/pr-workflow.md) — worktree + branch naming, the cross-repo (pack) clone procedure, PR title/body template, the one-candidate-per-PR rule, post-create status update.
