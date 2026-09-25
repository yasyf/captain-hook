from __future__ import annotations

from captain_hook import Allow, Event, Input, Tool, UserSaid, hook
from captain_hook.builtin_packs.graphite.hooks._lib import GraphiteRuns, HasFlag, JJReads, PushesTagRef, ReviewPassRan

# Inline tests are Allow-only: a FileFixture can't stage a nested `.git/.graphite_repo_config`, so
# GraphiteRuns() is always off at cwd="/" and every matcher short-circuits to allow. The fire path
# (marker present, in a real gt repo/worktree) is covered by tests/test_pack_graphite.py, and so are
# the two things inline rows cannot see: the ccx.nogt opt-out, and the JJReads carve-out, which
# matches_conditions collapses into the same allow a failed only_if produces.

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), GraphiteRuns(("jj",))],
    skip_if=[JJReads()],
    message=(
        "BLOCKED: the repository this command targets runs on Graphite (gt), not jj — its stack metadata "
        "lives in Graphite. Use `ccx vcs ship` to commit and submit, `ccx vcs stack new <name>` to cut a "
        "stacked branch, and `gt log` or `ccx vcs stack list` to inspect the stack."
    ),
    block=True,
    tests={
        Input(command="jj new", cwd="/"): Allow(),
        Input(command="jj commit -m x", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
        Input(command="jj log", cwd="/"): Allow(),
        Input(command="jj bookmark list", cwd="/"): Allow(),
        Input(command="cd /tmp && jj new", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(
            ("git", "commit"),
            ("git", "push"),
            ("git", "switch", "-c"),
            ("git", "switch", "-C"),
            ("git", "switch", "--create"),
            ("git", "checkout", "-b"),
            ("git", "checkout", "-B"),
        ),
    ],
    skip_if=[HasFlag("--dry-run"), HasFlag("--tags"), PushesTagRef()],
    message=(
        "Committing, branching, and pushing go through ccx vcs in the repository this command targets. "
        'Use `ccx vcs ship -m "<msg>"` to commit and submit, `ccx vcs stack new <name>` to cut a stacked '
        "branch, and `ccx vcs stack submit` to restack and resubmit the stack. Raw git writes leave "
        "Graphite's stack metadata stale."
    ),
    tests={
        Input(command="git commit -m x", cwd="/"): Allow(),
        Input(command="git push", cwd="/"): Allow(),
        Input(command="git switch -c feature", cwd="/"): Allow(),
        Input(command="git switch -C main", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
        Input(command="cd /tmp && git push", cwd="/"): Allow(),
        Input(command="git -C /tmp push", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(("gt", "submit"), ("gt", "s"), ("gt", "ss"), ("ccx", "vcs", "ship")),
    ],
    skip_if=[
        ReviewPassRan(),
        UserSaid(r"<command-name>/?cc-review", scope="session"),
        HasFlag("--dry-run"),
        HasFlag("--no-push"),
    ],
    message=(
        "Before submitting: no review pass has run this session — run one first (`/cc-review:start`), the "
        "single finder pass over the diff a non-trivial change carries. And PRs are always published, "
        "never draft: submit without `--draft`/`-d`."
    ),
    tests={
        Input(command="gt submit", cwd="/"): Allow(),
        Input(command="gt ss", cwd="/"): Allow(),
        Input(command="ccx vcs ship -m x", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(("git", "rebase"), ("git", "merge"), ("git", "pull")),
    ],
    skip_if=[HasFlag("--abort", "--continue", "--quit")],
    message=(
        "Use `ccx vcs stack submit`, which fetches trunk and replays every lane. A raw rebase, merge, "
        "or pull leaves Graphite's parent revision stale."
    ),
    tests={
        Input(command="git rebase main", cwd="/"): Allow(),
        Input(command="git pull", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)
