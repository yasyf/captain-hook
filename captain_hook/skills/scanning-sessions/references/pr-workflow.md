# PR Workflow

The mechanics behind Steps 3-7: one candidate, one branch, one PR. Never commit on the
user's checkout — their working tree may hold uncommitted work, and the PR must be
reviewable against the default branch, not their local state.

## The one-candidate-per-PR rule

Each PR encodes exactly one rule: one candidate, one hook file, one revert. A reviewer
must be able to merge the force-push guard while rejecting the logger nudge. The
repo-wide open-PR cap is enforced by `threshold-check`'s eligibility call — if three
candidates are eligible, all three got past the cap; open one PR each, never one PR for
all three.

## Worktree + branch

Branch naming: `capt-hook/review/<rule-slug>`, where `<rule-slug>` is the short
kebab-case name of the rule (it should match the hook file's slug:
`.claude/hooks/no_force_push.py` → `capt-hook/review/no-force-push`). Fix
candidates use `capt-hook/review/fix-<slug>`, where `<slug>` is the target hook
file's stem (`.claude/hooks/status_nudge.py` → `capt-hook/review/fix-status-nudge`).

```bash
default=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
git fetch origin "$default"
worktree=$(mktemp -d)/capt-hook-review
git worktree add -b "capt-hook/review/<rule-slug>" "$worktree" "origin/$default"
```

Hand `$worktree` to the `authoring-hooks` skill as the directory to write
`.claude/hooks/<slug>.py` in, and run all verification there.

## Verify, commit, push

```bash
cd "$worktree" && uvx capt-hook test     # must be green — skip the candidate otherwise
git -C "$worktree" add .claude/hooks/<slug>.py
git -C "$worktree" commit -m "feat(hooks): add <rule-slug> guard from session feedback"
git -C "$worktree" push -u origin "capt-hook/review/<rule-slug>"
```

Commit only the hook file — never settings, lockfiles, or anything else the worktree
picked up. A fix PR commits the **amended** target hook file (which now carries the
regression test) with
`fix(hooks): stop <slug> misfiring on <misfire-class> (regression-tested)`.

## PR title and body

Title: `[capt-hook] <imperative rule statement>` — e.g.
`[capt-hook] Block force-pushes to protected branches`.

Body template:

```markdown
## Rule

<one-sentence rule the corrections imply>

## Hook

`.claude/hooks/<slug>.py` — <primitive> on <event>; fires on <offending shape>, stays
silent on <benign neighbor>. Inline tests pass (`uvx capt-hook test`).

## Evidence

Corrections given in this repo's sessions, verbatim:

- "<verbatim correction>" — session `<session_id>`, <YYYY-MM-DD>
- "<verbatim correction>" — session `<session_id>`, <YYYY-MM-DD>

---
Opened by capt-hook's session reviewer (candidate #<ID>). Merging adopts the rule;
closing rejects it and the reviewer will not re-propose this candidate.
```

The Evidence section is the PR's case: every quote verbatim, each with its session id
and date taken from the Step-2 verification — the transcript file the quote was found
in names the session (the JSONL filename stem is the session id) and the matching
line's `timestamp` field gives the date.

A fix PR adapts the template: title `[capt-hook] Fix <slug> misfiring on
<misfire-class>`; the Rule section states what the hook wrongly fired on and the
amendment chosen (tightened condition, re-fire guard, live state, demoted severity, or
removal); the Hook section names the regression test pair (silent on the misfiring
input, still firing on the genuine case); the Evidence section quotes Claude's
verbatim complaints with their session ids and dates, plus the decision-ledger attribution
(`target_hook_name`, the fire's event/action, and its message).

```bash
gh pr create --title "<title>" --body "<body>" \
  --base "$default" --head "capt-hook/review/<rule-slug>"
```

## After creation

Stamp the candidate immediately — this is what frees the eligibility math from
double-proposing and lets `sync-prs` track the PR's fate:

```bash
uvx capt-hook review update <ID> pr_open --pr-url <url>
git worktree remove "$worktree" --force
```

A merged PR later moves the candidate to `accepted`, a closed one to `rejected` — both
via `review sync-prs`, not by you.
