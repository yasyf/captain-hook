from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Event,
    Input,
    UsedSkill,
    block_command,
    nudge,
)

block_command(
    ["git", "stash"],
    reason="git stash is not allowed",
    hint=(
        "In a jj repo you never need to stash — the working copy is commit @; use `jj new` "
        "to set it aside or `jj rebase` directly (no clean tree required). In plain git, "
        "commit your changes to a branch"
    ),
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
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
    skip_if=[UsedSkill("codex|codex:codex")],
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 2 and not evt.ctx.t.has_command(r"codex"),
)
