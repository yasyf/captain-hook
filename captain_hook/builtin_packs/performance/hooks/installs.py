from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, CommandMatches, CommandSchema, Event, Input, Or, PathMatches, Tool, hook
from captain_hook.cmd import INFO_OPTIONS, package_directory

if TYPE_CHECKING:
    from captain_hook import Arguments
    from captain_hook.cmd import Call

LANES = PathMatches(
    ("**/.claude/worktrees/**", "**/.orca/workspaces/**", "**/repos/claude/worktrees/**", "**/repos/orca/workspaces/**")
)
INSTALL_VERBS = {
    "yarn": frozenset({"install", "add", "remove", "up", "dedupe"}),
    "npm": frozenset({"install", "i", "add", "ci", "clean-install", "install-test", "it", "install-ci-test", "cit"}),
    "pnpm": frozenset({"install", "i", "add"}),
    "bun": frozenset({"install", "i", "add"}),
}
OUTSIDE_LANE = frozenset({"--mode=update-lockfile", "--package-lock-only", "--lockfile-only", "-g", "--global"})
INFO_FLAGS = INFO_OPTIONS | {"-v"}


def leaves_lane(call: Call) -> bool:
    return not OUTSIDE_LANE.isdisjoint(call.flags) or ("--mode", "update-lockfile") in pairwise(call.flags)


def installs(call: Call) -> bool:
    if leaves_lane(call) or not INFO_FLAGS.isdisjoint(call.flags):
        return False
    verb = next(iter(call.targets), None)
    return call.name == "yarn" if verb is None else verb.value in INSTALL_VERBS[call.name]


def installs_in_lane(arguments: Arguments) -> bool:
    return installs(arguments.call) and any(map(LANES, package_directory(arguments.call)))


hook(  # capt-hook: no-escape node_modules in a lane comes only from warm-lane, so no lane command may write it
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(*(CommandMatches(CommandSchema(program), only_if=(installs_in_lane,)) for program in INSTALL_VERBS)),
    ],
    message=(
        "A lane never installs JavaScript dependencies into its worktree. Run `tools/hooks/warm-lane.sh` to link "
        "`node_modules` to the shared store for the lockfile; a dependency change runs "
        "`yarn <verb> --mode=update-lockfile` first, and the changed lockfile rebuilds the store, never the lane."
    ),
    block=True,
    tests={
        Input(command="yarn install --immutable", cwd="/Users/alice/.claude/worktrees/monorepo/storage-config"): Block(
            pattern="warm-lane"
        ),
        Input(command="yarn", cwd="/Users/alice/repos/claude/worktrees/monorepo/lane"): Block(),
        Input(command="yarn --immutable", cwd="/Users/alice/repos/claude/worktrees/monorepo/lane"): Block(),
        Input(command="yarn add zod", cwd="/Users/alice/repos/claude/worktrees/monorepo/lane"): Block(),
        Input(command="yarn --cwd ~/.claude/worktrees/monorepo/lane install", cwd="/Users/alice"): Block(),
        Input(command="yarn --cwd=/Users/alice/.orca/workspaces/monorepo/lane install", cwd="/"): Block(),
        Input(command="npm install", cwd="/Users/alice/.orca/workspaces/monorepo/lane"): Block(),
        Input(command="npm i left-pad", cwd="/Users/alice/.orca/workspaces/monorepo/lane"): Block(),
        Input(command="npm ci", cwd="/Users/alice/repos/orca/workspaces/7f3c2a10/monorepo/lane"): Block(),
        Input(command="npm --prefix /Users/alice/.claude/worktrees/app/lane ci", cwd="/"): Block(),
        Input(command="pnpm install", cwd="/Users/alice/.claude/worktrees/app/lane"): Block(),
        Input(command="pnpm -C /Users/alice/.claude/worktrees/app/lane i", cwd="/"): Block(),
        Input(command="pnpm --filter web install", cwd="/Users/alice/.claude/worktrees/app/lane"): Block(),
        Input(command="bun install", cwd="/Users/alice/repos/claude/worktrees/app/lane"): Block(),
        Input(command="bun i", cwd="/Users/alice/repos/claude/worktrees/app/lane"): Block(),
        Input(command="git pull && yarn install", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Block(),
        Input(command="sh -c 'yarn install'", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Block(),
        Input(command="yarn biome check src", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(command="yarn tsc --noEmit -p .", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(command="yarn run lint", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(command="yarn -v", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(command="npm install --help", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(
            command="yarn install --mode=update-lockfile", cwd="/Users/alice/.claude/worktrees/monorepo/lane"
        ): Allow(),
        Input(
            command="yarn add zod --mode update-lockfile", cwd="/Users/alice/.claude/worktrees/monorepo/lane"
        ): Allow(),
        Input(command="npm install -g pnpm", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(command="tools/hooks/warm-lane.sh", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(command="yarn install --immutable", cwd="/Users/alice/Code/monorepo"): Allow(),
        Input(command="cd / && yarn install", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
        Input(
            command="yarn --cwd /Users/alice/Code/monorepo install", cwd="/Users/alice/.claude/worktrees/monorepo/lane"
        ): Allow(),
        Input(command="yarn install --immutable", cwd="/Users/alice/.vcache/nm-store/3f9a1c"): Allow(),
        Input(command="yarn install --immutable", cwd="/Users/alice/repos/.cache/nm-store/3f9a1c"): Allow(),
        Input(command="echo yarn install", cwd="/Users/alice/.claude/worktrees/monorepo/lane"): Allow(),
    },
)
