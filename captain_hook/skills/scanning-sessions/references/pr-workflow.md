# PR Workflow

The mechanics behind Steps 3-7: one candidate, one branch, one PR. Never commit on the
user's checkout — their working tree may hold uncommitted work, and the PR must be
reviewable against the default branch, not their local state.

## The one-candidate-per-PR rule

Each PR encodes exactly one rule: one candidate, one hook file, one revert. A reviewer
must be able to merge the force-push guard while rejecting the logger nudge. The
repo-wide open-PR cap is enforced twice — by `threshold-check`'s eligibility call when
the reviewer spawns, and by the `review slots` check before each `gh pr create`, since
earlier PRs in the same pass can fill the cap. A candidate the cap squeezes out is a
logged skip; never batch several candidates into one PR to fit under it.

## Worktree + branch

Branch naming: `capt-hook/review/<rule-slug>`, where `<rule-slug>` is the candidate's
`rule` field, used verbatim. Post-judging, a create candidate's `rule` IS a canonical
kebab-case slug — the judge assigns it and the closing regroup re-parents evidence onto
it — so the branch name comes straight from that field, not from re-deriving a slug off
a hook filename. It lines up with the hook file's slug by construction: a candidate
whose `rule` is `no-force-push` writes `.claude/hooks/no_force_push.py` on branch
`capt-hook/review/no-force-push`. Fix candidates use `capt-hook/review/fix-<slug>`,
where `<slug>` is the target hook file's stem (so `.claude/hooks/status_nudge.py`
becomes `capt-hook/review/fix-status-nudge`). A **create-as-edit** — a create
candidate Step 3 routed into an existing hook — keeps `capt-hook/review/<rule-slug>`
even when the PR targets a pack repo: the recovery procedure in "After creation"
matches successor candidates by `rule` against the branch slug, and a `fix-` or
`pack-` prefix would break it.

```bash
default=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
git fetch origin "$default"
worktree=$(mktemp -d)/capt-hook-review
git worktree add -b "capt-hook/review/<rule-slug>" "$worktree" "origin/$default"
```

Hand `$worktree` to the `authoring-hooks` skill as the directory to write
`.claude/hooks/<slug>.py` in, and run all verification there.

## Cross-repo (pack) edit

A PR that amends a pack hook opens against the pack's own repo — never the watched
repo the candidate was mined in. Two paths arrive here: a **fix** candidate whose
`review show` prints a `routing:` line (the PR opens against its `target_repo`), and
a **create-as-edit** — a create candidate whose Step-3 overlap check upgraded it to a
universal edit of a pack hook. A create candidate's **new** hook file never lands
here; new hooks are always repo-local.

The watched repo's checkout has no remote for the pack repo, so clone instead of
adding a worktree:

```bash
clone=$(mktemp -d)/pack-repo
gh repo clone <target_repo> "$clone"
default=$(gh repo view <target_repo> --json defaultBranchRef -q .defaultBranchRef.name)
git -C "$clone" switch -c "<branch>" "origin/$default"
```

`<branch>` follows the candidate kind: a fix uses `capt-hook/review/pack-<slug>`,
where `<slug>` is the target hook file's stem (kebab-case); a create-as-edit keeps
`capt-hook/review/<rule-slug>`. Run the target-hook re-verification
(`git cat-file -e`, the registration `rg`) against this clone's `origin/$default` —
the file lives here. When it fails, a fix is skipped; a create-as-edit falls back to
a new repo-local hook in the watched repo.

Verification runs in the clone and differs by pack kind:

- **Builtin pack** (`target_repo` is captain-hook): the hook lives under
  `captain_hook/packs/<pack>/`; verify with `uv run --project . capt-hook test`.
- **External pack**: the hook lives in the directory named by the `hooks` key in the
  `[pack]` table of the clone's `capt-hook.toml` manifest; verify with
  `uvx --isolated capt-hook --hooks <dir> test` against that directory.

Commit, push, and `gh pr create` run inside the clone with the same templates as
below — the commit-message and PR-body shapes for the candidate's kind carry over
unchanged, and the post-create stamp (`review update <ID> pr_open --pr-url <url>`) is
identical. The pre-create slot check targets the pack repo —
`review slots --repo <target_repo>` — not the watched repo. If the
push is **denied** (no write access to the pack repo), log the skip with its reason in
the final report and leave the candidate `watching` — never commit the change into the
watched repo instead: a pack hook patched or copied locally diverges from the pack and
re-breaks on its next update. Clean up with `rm -rf` on the temp dir; there is no
worktree to remove.

## Verify, commit, push

```bash
cd "$worktree" && uvx --isolated capt-hook test     # must be green — skip the candidate otherwise
```

Then confirm the target repo still has a free PR slot before committing anything —
eligibility was computed when the reviewer spawned, and a concurrent pass may have
filled the cap since:

```bash
uvx --isolated capt-hook review slots --repo <target_repo_key>
```

When it exits 1 (`free=0`), log the skip and leave the candidate `watching` — no
commit, no push, no branch left behind; the slot frees when an open PR merges,
closes, or goes stale. Output format and exit semantics: [review CLI](review-cli.md).

```bash
git -C "$worktree" add .claude/hooks/<slug>.py
git -C "$worktree" commit -m "feat(hooks): add <rule-slug> guard from session feedback"
git -C "$worktree" push -u origin "<branch>"
```

Commit only the hook file — never settings, lockfiles, or anything else the worktree
picked up. A fix PR commits the **amended** target hook file (which now carries the
regression test) with
`fix(hooks): stop <slug> misfiring on <misfire-class> (regression-tested)`. A
create-as-edit commits the amended hook file with
`feat(hooks): broaden <hook-slug>: <imperative rule>` — feat-flavored, naming both
the amended hook and the mined rule.

## PR title and body

Title: `[capt-hook] <imperative rule statement>` — e.g.
`[capt-hook] Block force-pushes to protected branches`.

Body template:

```markdown
## Rule

<one-sentence rule the corrections imply>

## Hook

`.claude/hooks/<slug>.py` — <primitive> on <event>; fires on <offending shape>, stays
silent on <benign neighbor>. Inline tests pass (`uvx --isolated capt-hook test`).

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

A create-as-edit PR adapts it differently: title `[capt-hook] Broaden <hook-slug>:
<imperative rule statement>`; the Hook section names the edited hook file
(repo-relative), what broadened (condition, pattern, or carve-out), and the new test
pair (fires on the newly covered shape, `Allow()` on its benign neighbor, all
pre-existing tests untouched and green); Evidence is unchanged. When the edit
targets a **pack** repo, the Rule section additionally carries a one-sentence
universality justification — why the broadening is correct for every consumer of
the pack; a body you cannot write that sentence for is a PR that should not exist
(route repo-local instead) — and the footer notes the rule was mined from one
repo's sessions and is proposed as universal, so the pack maintainer can reject on
that axis alone. A repo-local edit needs neither; the repo's hooks are its own.

```bash
gh pr create --title "<title>" --body "<body>" \
  --base "$default" --head "<branch>"
```

## After creation

Stamp the candidate immediately — this is what frees the eligibility math from
double-proposing and lets `sync-prs` track the PR's fate:

```bash
uvx --isolated capt-hook review update <ID> pr_open --pr-url <url>
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
`uvx --isolated capt-hook review list --repo <key>` — `<key>` is the **watched repo's** key, the
candidate row's home even when the PR targeted a pack — and stamp the successor create candidate whose
`rule` equals the branch's slug — `uvx --isolated capt-hook review update <successor-ID> pr_open
--pr-url <url>`. If no such candidate exists (the rule was judge-retired), close the PR
you just opened — `sync-prs` only follows PRs a `pr_open` candidate row points at, so an
unstamped PR would sit open forever with nothing tracking its fate:

```bash
gh pr close <url> --delete-branch \
  --comment "capt-hook: the backing candidate was judge-retired before this PR could be tracked; closing rather than leaving it orphaned."
```

Note the closed PR in the final report and move on.
