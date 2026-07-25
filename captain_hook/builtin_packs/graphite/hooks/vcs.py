from __future__ import annotations

from captain_hook import (
    Allow,
    Event,
    Input,
    Or,
    Runs,
    Tool,
    hook,
)
from captain_hook.builtin_packs.graphite.hooks._lib import GraphiteActive, HasFlag, PushesTagRef, ReviewPassRan

# Inline tests are Allow-only: a FileFixture can't stage a nested `.git/.graphite_repo_config`, so
# GraphiteActive() is always off at cwd="/" and every matcher short-circuits to allow. The fire path
# (marker present, in a real gt repo/worktree) is covered by tests/test_pack_graphite.py.

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), Runs("jj"), GraphiteActive()],
    message=(
        "BLOCKED: this repository's workflow is Graphite (gt), not jj — its stack metadata lives in "
        'Graphite. Use `gt create -m "<msg>"` to start a stacked branch, `gt modify` to amend, `gt log` '
        "to inspect the stack, and `gt checkout` to move between branches."
    ),
    block=True,
    tests={
        Input(command="jj new", cwd="/"): Allow(),
        Input(command="jj commit -m x", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(
            Runs("git", "commit"),
            Runs("git", "push"),
            Runs("git", "switch", "-c"),
            Runs("git", "switch", "-C"),
            Runs("git", "switch", "--create"),
            Runs("git", "checkout", "-b"),
            Runs("git", "checkout", "-B"),
        ),
        GraphiteActive(),
    ],
    skip_if=[HasFlag("--dry-run"), HasFlag("--tags"), PushesTagRef()],
    message=(
        "Committing, branching, and pushing go through gt in this repository — `gt create -m` starts a "
        "stacked branch, `gt modify` amends and restacks, `gt submit` opens/updates PRs. Prefer "
        '`ccx vcs ship -m "<msg>"`, which drives the whole gt lane. Raw git rewrites leave Graphite\'s '
        "stack metadata stale."
    ),
    tests={
        Input(command="git commit -m x", cwd="/"): Allow(),
        Input(command="git push", cwd="/"): Allow(),
        Input(command="git switch -c feature", cwd="/"): Allow(),
        Input(command="git switch -C main", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(Runs("gt", "submit"), Runs("gt", "s"), Runs("gt", "ss"), Runs("ccx", "vcs", "ship")),
        GraphiteActive(),
    ],
    skip_if=[
        ReviewPassRan(),
        HasFlag("--dry-run"),
        HasFlag("--no-push"),
    ],
    message=(
        "Before submitting: has the user explicitly approved publishing these PRs? Approval to write the "
        "code is not approval to submit it. No review pass has run this session — offer one first "
        "(`/cc-review:start`) so the user can review the diff before it goes up. And PRs are always "
        "published, never draft: submit without `--draft`/`-d`."
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
        Or(Runs("git", "rebase"), Runs("git", "merge"), Runs("git", "pull")),
        GraphiteActive(),
    ],
    skip_if=[HasFlag("--abort", "--continue", "--quit")],
    message=(
        "Prefer `gt restack` / `gt sync` — a raw rebase, merge, or pull desyncs Graphite's stack "
        "metadata, and `ccx vcs ship` refuses to submit a stack that needs a restack."
    ),
    tests={
        Input(command="git rebase main", cwd="/"): Allow(),
        Input(command="git pull", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)
