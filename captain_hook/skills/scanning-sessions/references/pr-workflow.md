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

Branch naming: `capt-hook/review/<rule-slug>`, where `<rule-slug>` is the candidate's
`rule` field, used verbatim. Post-judging, a create candidate's `rule` IS a canonical
kebab-case slug — the judge assigns it and the closing regroup re-parents evidence onto
it — so the branch name comes straight from that field, not from re-deriving a slug off
a hook filename. It lines up with the hook file's slug by construction: a candidate
whose `rule` is `no-force-push` writes `.claude/hooks/no_force_push.py` on branch
`capt-hook/review/no-force-push`. Fix candidates use `capt-hook/review/fix-<slug>`,
where `<slug>` is the target hook file's stem (so `.claude/hooks/status_nudge.py`
becomes `capt-hook/review/fix-status-nudge`).

```bash
default=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
git fetch origin "$default"
worktree=$(mktemp -d)/capt-hook-review
git worktree add -b "capt-hook/review/<rule-slug>" "$worktree" "origin/$default"
```

Hand `$worktree` to the `authoring-hooks` skill as the directory to write
`.claude/hooks/<slug>.py` in, and run all verification there.

## Cross-repo (pack) fix

When `review show` prints a `routing:` line, the PR opens against `target_repo` — the
pack's own repo — never the watched repo the misfire fired in. The same procedure
carries a create candidate classified as generic: its target is captain-hook and its
hook lands under `captain_hook/packs/general/`.

The watched repo's checkout has no remote for the pack repo, so clone instead of
adding a worktree:

```bash
clone=$(mktemp -d)/pack-repo
gh repo clone <target_repo> "$clone"
default=$(gh repo view <target_repo> --json defaultBranchRef -q .defaultBranchRef.name)
git -C "$clone" switch -c "capt-hook/review/pack-<slug>" "origin/$default"
```

Branch naming: `capt-hook/review/pack-<slug>`, where `<slug>` is the target hook
file's stem (kebab-case) for a fix, or the candidate's `rule` for a general-pack
create. Run the fix candidate's target-hook re-verification (`git cat-file -e`, the
registration `rg`) against this clone's `origin/$default` — the file lives here.

Verification runs in the clone and differs by pack kind:

- **Builtin pack** (`target_repo` is captain-hook): the hook lives under
  `captain_hook/packs/<pack>/`; verify with `uv run --project . capt-hook test`.
- **External pack**: the hook lives in the directory named by the `hooks` key of the
  clone's `capt-hook.toml` manifest; verify with `uvx capt-hook --hooks <dir> test`
  against that directory.

Commit, push, and `gh pr create` run inside the clone with the same templates as
below — the fix commit message and PR body shapes carry over unchanged, and the
post-create stamp (`review update <ID> pr_open --pr-url <url>`) is identical. If the
push is **denied** (no write access to the pack repo), log the skip with its reason in
the final report and leave the candidate `watching` — never commit the fix into the
watched repo instead: a pack hook patched locally diverges from the pack and re-breaks
on its next update. Clean up with `rm -rf` on the temp dir; there is no worktree to
remove.

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

Then tell the user — on macOS, fire a desktop notification carrying the PR url. Best-effort:
a notification failure never fails the run.

```bash
[ "$(uname)" = Darwin ] && osascript -e 'display notification "<url>" with title "capt-hook review"' || true
```

A merged PR later moves the candidate to `accepted`, a closed one to `rejected` — both
via `review sync-prs`, not by you.

**If `review update` fails, recover in place — never abort the run over it.** A concurrent
judge pass regrouped the candidate out from under you, realistic when several sessions
share one review database. Two shapes: `no candidate with id <ID>` means a summary-to-full
re-judge re-parented the candidate's observations onto a fresh slug candidate and the
emptied original was swept; a transition error like `rejected -> pr_open` means the
candidate was judge-retired (every observation re-judged, none accepted). Re-run
`uvx capt-hook review list --repo <key>` and stamp the successor create candidate whose
`rule` equals the branch's slug — `uvx capt-hook review update <successor-ID> pr_open
--pr-url <url>`. If no such candidate exists (the rule was judge-retired), close the PR
you just opened — `sync-prs` only follows PRs a `pr_open` candidate row points at, so an
unstamped PR would sit open forever with nothing tracking its fate:

```bash
gh pr close <url> --delete-branch \
  --comment "capt-hook: the backing candidate was judge-retired before this PR could be tracked; closing rather than leaving it orphaned."
```

Note the closed PR in the final report and move on.
