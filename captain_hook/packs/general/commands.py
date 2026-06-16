from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    Input,
    Tool,
    UsedSkill,
    block_command,
    hook,
    nudge,
)

block_command(
    ["git", "stash"],
    reason="git stash is not allowed",
    hint="Commit your changes to a branch instead",
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
    },
)


class UnpipedGrep(CustomCondition):
    """True when a `grep` command does not consume piped input.

    Allows the stream-filter idiom (`… | grep`) while still blocking grep used
    for file searching, whether standalone, heading a pipe, or in a `&&`/`;` chain.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        if not (cl := evt.command_line):
            return False
        return any(
            cmd.matches(r"^grep\b") and (i == 0 or cl.parts[i - 1][1] != "|") for i, (cmd, _) in enumerate(cl.parts)
        )


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), UnpipedGrep()],
    message="BLOCKED: Use ripgrep (rg) instead of grep. Replace grep with rg, or use the built-in Grep tool.",
    block=True,
    tests={
        Input(command="grep -rn foo src/"): Block(),
        Input(command="ls | grep foo"): Allow(),
        Input(command="cat x | grep foo | sort"): Allow(),
        Input(command="grep foo file.py | wc -l"): Block(),
        Input(command="grep foo a && echo done"): Block(),
        Input(command="git log --grep=fix"): Allow(),
        Input(command='git log --grep "fix bug"'): Allow(),
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
