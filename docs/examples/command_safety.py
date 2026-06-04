from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Event,
    HookResult,
    InlineTests,
    Input,
    Tool,
    block_command,
    on,
)

GIT_STASH_TESTS: InlineTests = {
    Input(command="git stash"): Block(),
    Input(command="git stash pop"): Block(),
    Input(command="git status"): Allow(),
}

FORCE_PUSH_TESTS: InlineTests = {
    Input(command="git push --force origin main"): Block(),
    Input(command="git push --force-with-lease"): Allow(),
    Input(command="git push origin main"): Allow(),
}

RM_RF_TESTS: InlineTests = {
    Input(command="rm -rf /"): Block(),
    Input(command="rm -rf build/"): Block(),
    Input(command="rm file.txt"): Allow(),
}

block_command(
    ["git", "stash"],
    reason="Use the team's VCS workflow for shelving changes",
    hint="Commit a WIP change instead of stashing",
    tests=GIT_STASH_TESTS,
)

block_command(
    r"git\s+push\s+--force(?!-)",
    reason="Force-push rewrites remote history",
    hint="Use --force-with-lease for a safer push",
    tests=FORCE_PUSH_TESTS,
)

block_command(
    ["rm", "-rf", "*"],
    reason="Recursive force-delete is forbidden",
    hint="Delete files individually or stage them to a trash directory",
    tests=RM_RF_TESTS,
)


@on(Event.PreToolUse, only_if=[Tool("Bash")])
def block_piped_curl_to_shell(evt: BaseHookEvent) -> HookResult | None:
    cl = evt.command_line
    if cl and cl.q.runs("curl") and cl.q.uses_redirect() and cl.q.any_command(lambda c: c.program in {"sh", "bash"}):
        return evt.block("BLOCKED: piping curl into a shell executes untrusted remote code.")
    return None


@on(Event.PreToolUse, only_if=[Tool("Bash")])
def block_dotenv_secrets_leak(evt: BaseHookEvent) -> HookResult | None:
    cl = evt.command_line
    if cl and cl.q.has_subcommand(".env") and cl.q.any_command(lambda c: c.program in {"cat", "echo", "printenv"}):
        return evt.block("BLOCKED: printing .env files leaks secrets into the transcript.")
    return None
