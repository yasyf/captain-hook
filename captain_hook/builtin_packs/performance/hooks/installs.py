from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, CommandMatches, CommandSchema, Event, FileFixture, Input, Or, Tool, hook
from captain_hook.cmd import INFO_OPTIONS, package_directory
from captain_hook.layout import lane_checkout

if TYPE_CHECKING:
    from captain_hook import Arguments
    from captain_hook.cmd import Call, Target

WARM_LANE = Path("tools", "hooks", "warm-lane.sh")
TEST_POOLS = {"HOOKS_LAYOUT_WORKTREE_POOLS": '{"~/lanes": 1}'}
INSTALL_VERBS = {
    "yarn": frozenset({"install", "add", "remove", "up", "dedupe"}),
    "npm": frozenset(
        {
            "install",
            "i",
            "in",
            "ins",
            "inst",
            "insta",
            "instal",
            "isnt",
            "isnta",
            "isntal",
            "isntall",
            "add",
            "ci",
            "clean-install",
            "ic",
            "install-clean",
            "isntall-clean",
            "install-test",
            "it",
            "install-ci-test",
            "cit",
        }
    ),
    "pnpm": frozenset({"install", "i", "add"}),
    "bun": frozenset({"install", "i", "add"}),
}
OUTSIDE_LANE = frozenset({"--package-lock-only", "--lockfile-only", "-g", "--global", "--dry-run"})
INFO_FLAGS = INFO_OPTIONS | {"-v"}


def options(call: Call) -> set[tuple[str, str]]:
    return {(name, value) for name, _, value in (flag.partition("=") for flag in call.flags)} | set(
        pairwise(call.flags)
    )


def leaves_lane(call: Call) -> bool:
    return ("--mode", "update-lockfile") in (pairs := options(call)) or any(
        name in OUTSIDE_LANE and value in {"", "true"} for name, value in pairs
    )


def installs(call: Call) -> bool:
    if leaves_lane(call) or not INFO_FLAGS.isdisjoint(call.flags):
        return False
    verb = next(iter(call.targets), None)
    return call.name == "yarn" if verb is None else verb.value in INSTALL_VERBS[call.name]


def warm_lane(target: Target) -> bool:
    return (
        (path := target.path) is not None
        and (checkout := lane_checkout(path)) is not None
        and (checkout / WARM_LANE).is_file()
    )


def installs_in_lane(arguments: Arguments) -> bool:
    return installs(arguments.call) and any(map(warm_lane, package_directory(arguments.call)))


def in_lane(command: str, *, lane: str = "lanes/monorepo/lane", env: dict[str, str] | None = None) -> Input:
    return Input(
        command=f"cd ~/{lane} && {command}",
        file=FileFixture(home=True, name=f"lanes/monorepo/lane/{WARM_LANE}", content=""),
        env=TEST_POOLS if env is None else env,
    )


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
        in_lane("yarn install --immutable"): Block(pattern="warm-lane"),
        in_lane("yarn"): Block(),
        in_lane("yarn --immutable"): Block(),
        in_lane("yarn add zod"): Block(),
        in_lane("yarn --cwd ~/lanes/monorepo/lane install", lane="."): Block(),
        in_lane("yarn --cwd=$HOME/lanes/monorepo/lane install", lane="."): Block(),
        in_lane("npm install"): Block(),
        in_lane("npm i left-pad"): Block(),
        in_lane("npm ci"): Block(),
        in_lane("npm --prefix ~/lanes/monorepo/lane ci", lane="."): Block(),
        in_lane("npm --prefix=$HOME/lanes/monorepo/lane install", lane="."): Block(),
        in_lane("pnpm install"): Block(),
        in_lane("pnpm -C ~/lanes/monorepo/lane i", lane="."): Block(),
        in_lane("pnpm --filter web install"): Block(),
        in_lane("bun install"): Block(),
        in_lane("bun i"): Block(),
        in_lane("git pull && yarn install"): Block(),
        in_lane("sh -c 'yarn install'"): Block(),
        in_lane("npm in"): Block(),
        in_lane("npm ic"): Block(),
        in_lane("npm --registry https://registry.npmjs.org install"): Block(),
        in_lane("npm install --package-lock-only=false"): Block(),
        in_lane("npm install --global=false zx"): Block(),
        in_lane("yarn install", lane="lanes/monorepo/lane/web"): Block(),
        in_lane("yarn install", env={"HOOKS_LAYOUT_WORKTREE_POOLS": '{"~/lanes/monorepo": 0}'}): Block(),
        in_lane("yarn install", env={}): Allow(),
        in_lane("yarn install", env={"HOOKS_LAYOUT_WORKTREE_POOLS": '{"~/elsewhere": 1}'}): Allow(),
        in_lane("yarn install", lane="lanes/monorepo"): Allow(),
        in_lane("yarn install", lane="lanes/app/lane"): Allow(),
        in_lane("yarn biome check src"): Allow(),
        in_lane("yarn tsc --noEmit -p ."): Allow(),
        in_lane("yarn run lint"): Allow(),
        in_lane("yarn -v"): Allow(),
        in_lane("npm install --help"): Allow(),
        in_lane("yarn install --mode=update-lockfile"): Allow(),
        in_lane("yarn add zod --mode update-lockfile"): Allow(),
        in_lane("npm install -g pnpm"): Allow(),
        in_lane("npm install --global=true pnpm"): Allow(),
        in_lane("npm install --package-lock-only=true"): Allow(),
        in_lane("npm install --dry-run"): Allow(),
        in_lane("cd -- / && npm install"): Allow(),
        in_lane("cd / && yarn install"): Allow(),
        in_lane("tools/hooks/warm-lane.sh"): Allow(),
        in_lane("yarn --cwd ~/src/monorepo install"): Allow(),
        in_lane("echo yarn install"): Allow(),
        Input(command="yarn install --immutable", cwd="/Users/alice/src/monorepo", env=TEST_POOLS): Allow(),
    },
)
