from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Event,
    Input,
    Or,
    RanCommand,
    Runs,
    Tool,
    UsedSkill,
    hook,
    nudge,
)

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), Runs("git", "stash")],
    message=(
        "BLOCKED: git stash is not allowed. In a jj repo you never need to stash — the working copy "
        "is commit @; use `jj new` to set it aside or `jj rebase` directly (no clean tree required). "
        "In plain git, commit your changes to a branch."
    ),
    block=True,
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
    },
)

hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(Runs("jj", "op", "restore"), Runs("jj", "operation", "restore"), Runs("jj", "undo")),
    ],
    message=(
        "BLOCKED: jj op restore and jj undo rewrite the whole repo to an earlier operation and can "
        "clobber everything since. Inspect instead: `jj op log` to find the operation, `jj op show <op>` "
        "or `jj op diff --op <op>` to see what it changed, and any read command against that state via "
        "`jj --at-op=<op> ...` (e.g. `jj --at-op=<op> st`, `jj --at-op=<op> file show -r <rev> <path>`). "
        "To recover content without time-travel: `jj restore --from <commit> <path>` for one file (hidden "
        "commits stay addressable by full ID via `jj --at-op=<op> log`), or materialize the old state in "
        "a throwaway workspace: `jj --at-op=<op> workspace add <dir> -r <rev>`. If a true restore is "
        "needed, stop and ask the user to run it."
    ),
    block=True,
    tests={
        Input(command="jj op restore abc123"): Block(),
        Input(command="jj operation restore abc123"): Block(),
        Input(command="jj undo"): Block(),
        Input(command="jj op log"): Allow(),
        Input(command="jj op show"): Allow(),
        Input(command="jj --at-op=abc123 log"): Allow(),
    },
)


# Requires the codex plugin (/plugin install codex@skills from yasyf/cc-skills).
# Delete this nudge if you don't use Codex.
nudge(
    """
    Multiple tool failures detected without a /codex invocation. After 2 failed
    approaches, get a second opinion from `/codex` before attempting a 3rd —
    Codex catches errors that Claude may miss.
    """,
    skip_if=[UsedSkill("codex"), RanCommand("codex")],
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 2,
)
