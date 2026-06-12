---
name: scanning-sessions
description: The headless session-reviewer brain — turns a watched repo's PR-eligible correction candidates into pull requests. Invoked as /scanning-sessions --transcript <path> inside the target repo by capt-hook's detached SessionEnd reviewer pipeline. Enumerates judge-accepted, threshold-eligible candidates via uvx capt-hook review (the CLI is the source of truth), re-verifies every cited correction verbatim against its session transcript, then per candidate drafts the hook in a worktree by delegating to the authoring-hooks skill, proves it with uvx capt-hook test, opens exactly one PR with the verbatim evidence, and records the PR on the candidate. Use when a prompt starts with /scanning-sessions, or to review eligible capt-hook candidates and open hook PRs.
argument-hint: "--transcript <path to the ended session's transcript>"
allowed-tools: Read, Grep, Glob, Bash, Skill
---

# Scanning Sessions into Hook PRs

You are the session reviewer's brain. capt-hook's detached SessionEnd pipeline spawned
you inside a watched repo because at least one correction candidate crossed its PR
thresholds. Your job: for each eligible candidate, draft the hook it implies, prove it,
and open **one** pull request carrying the user's verbatim corrections as evidence.

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
- **All writes happen in a worktree off `origin/<default>`** — never commit on the
  user's checkout. Procedure: [PR workflow](references/pr-workflow.md).
- **You do not draft hooks yourself** — this skill carries no Write/Edit. Drafting
  happens inside the `authoring-hooks` skill, invoked via the Skill tool.
- **`uvx capt-hook test` must be green** in the worktree before `gh pr create`.
- **Run to completion — never stop early.** You run headless; a text-only reply ends
  the session immediately. After the authoring-hooks skill returns, keep going in the
  same run: the job is done only when every eligible candidate either has
  `uvx capt-hook review update <id> pr_open --pr-url <url>` recorded for a created PR
  or has been explicitly skipped with a logged reason. Summaries come last, after
  Step 7 — never between steps.

## Workflow

Copy this checklist into your response and check off steps as you complete them:

```
Review Progress:
- [ ] Step 1: Enumerate eligible candidates (threshold-check / list / show)
- [ ] Step 2: Re-verify each candidate's quotes against transcripts
- [ ] Per eligible, verified candidate:
  - [ ] Step 3: Worktree off origin/<default>
  - [ ] Step 4: Draft via the authoring-hooks skill
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

### 3-7. One PR per candidate

Follow [references/pr-workflow.md](references/pr-workflow.md) exactly:

1. **Worktree** — fetch, then `git worktree add` a `capt-hook/review/<rule-slug>`
   branch off `origin/<default>`.
2. **Draft** — invoke the `authoring-hooks` skill via the Skill tool, passing the
   verbatim correction, its context, and the worktree path. It picks the primitive,
   writes `.claude/hooks/<slug>.py` with inline tests (one firing on the offending
   shape, one `Allow()` on a benign neighbor), and runs `uvx capt-hook test`.
3. **Verify** — run `uvx capt-hook test` in the worktree yourself; green or the
   candidate is skipped this pass.
4. **PR** — commit the hook file, push the branch, `gh pr create` with the template
   body: the rule, the hook's behavior, and an Evidence section quoting each verbatim
   correction with its session id and date.
5. **Record** — `uvx capt-hook review update <ID> pr_open --pr-url <url>`, then remove
   the worktree.

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
- [PR workflow](references/pr-workflow.md) — worktree + branch naming, PR title/body template, the one-candidate-per-PR rule, post-create status update.
