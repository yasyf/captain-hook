from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from captain_hook import BaseHookEvent, CustomCommandLineCondition, CustomCondition, FileFixture, Input
from captain_hook.cmd import git_location
from captain_hook.util.vcs import graphite_lane, graphite_lane_of_git_dir

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

    from captain_hook.cmd import Call

FIXTURE_GITDIR = Path(__file__).parents[1] / "gitdir"
GRAPHITE_QUEUES = frozenset({"", "graphite"})


def graphite_owns(call: Call, session_cwd: Path | None) -> bool:
    cwd, git_dir = git_location(call, session_cwd)
    if git_dir is not None:
        return graphite_lane_of_git_dir(git_dir)
    return cwd is not None and graphite_lane(cwd)


def graphite_queue(call: Call, session_cwd: Path | None) -> bool:
    cwd, git_dir = git_location(call, session_cwd)
    if cwd is None:
        return True
    location = ["--git-dir", str(git_dir)] if git_dir is not None else []
    probe = subprocess.run(
        ["git", *location, "config", "--get", "ccx.queue"],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    if probe.returncode == 0:
        return probe.stdout.strip() in GRAPHITE_QUEUES
    root = next((ancestor for ancestor in (cwd, *cwd.parents) if (ancestor / ".git").exists()), cwd)
    config = root / ".claude" / "ccx.toml"
    return (
        not config.is_file()
        or tomllib.loads(config.read_text()).get("vcs", {}).get("queue", "").strip() in GRAPHITE_QUEUES
    )


@dataclass(frozen=True, slots=True)
class GraphiteRuns(CustomCommandLineCondition):
    """Matches when one call runs one of ``argvs`` inside a repository Graphite owns.

    Ownership and verb are judged on the same call, where it runs: after a leading ``cd``, or
    through git's own ``-C`` and ``--git-dir`` global options (``--git-dir`` resolves against the
    ``-C`` cwd, as git does), never at the session cwd unless the call's own cannot be resolved
    (``cd $OTHER``). So ``cd ../plain-repo && git push`` from a Graphite session is left alone,
    ``cd ../gt-repo && git push`` from a plain-git session is not, and an unrelated
    ``git -C ../gt-repo status`` never lends its repository to a ``jj new`` beside it. A live
    ``gt repo init`` marker is necessary but not sufficient: a repository that sets ``ccx.nogt``
    has opted out of the gt lane — ccx itself declines it there — so a stale marker must not
    make these hooks steer toward gt.
    """

    argvs: tuple[tuple[str, ...], ...]

    def __init__(self, *argvs: tuple[str, ...]) -> None:
        object.__setattr__(self, "argvs", argvs)

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(
            any(call.verb_argv[: len(argv)] == argv for argv in self.argvs) and graphite_owns(call, evt.cwd)
            for call in evt.cmd.calls()
        )


@dataclass(frozen=True, slots=True)
class HasFlag(CustomCommandLineCondition):
    """Matches when any command in the parsed line carries one of the given flags.

    Structural, unlike ``Command(regex)``: a flag mentioned inside a quoted message body
    (``git commit -m "add --tags support"``) never matches.
    """

    flags: frozenset[str]

    def __init__(self, *flags: str) -> None:
        object.__setattr__(self, "flags", frozenset(flags))

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(not self.flags.isdisjoint(flag.split("=")[0] for flag in call.flags) for call in evt.cmd.calls())


class PushesTagRef(CustomCommandLineCondition):
    """Matches a ``git push`` whose refspec operand targets ``refs/tags``."""

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(
            call.name == "git"
            and call.targets
            and call.targets.targets[0].value == "push"
            and any((t.value or "").startswith("refs/tags") for t in call.targets.targets[1:])
            for call in evt.cmd.calls()
        )


class CcxInstalled(CustomCondition):
    """Matches when ``ccx`` is on PATH, so its stack verbs are there to route raw gt and git writes to."""

    def check(self, evt: BaseHookEvent) -> bool:
        return shutil.which("ccx") is not None


def git_probe(call: Call, session_cwd: Path | None, *args: str) -> str | None:
    """Run a read-only ``git`` query against the repository ``call`` targets; ``None`` when git exits non-zero."""
    cwd, git_dir = git_location(call, session_cwd)
    if cwd is None:
        return None
    location = ["--git-dir", str(git_dir)] if git_dir is not None else []
    probe = subprocess.run(
        ["git", "-C", str(cwd), *location, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return probe.stdout.strip() if probe.returncode == 0 else None


def rebases_onto_own_upstream(call: Call, session_cwd: Path | None) -> bool:
    """Whether a ``git rebase`` replays the branch onto its own remote-tracking ref.

    That is the manual recovery ``ccx vcs ship`` prints when the remote branch moved under it
    (``git rebase --autostash origin/<branch>``); it rewrites nothing of the stack's shape.
    """
    operands = call.targets
    if not operands.complete or any(flag.split("=", 1)[0] == "--onto" for flag in call.flags):
        return False
    upstream = [target.value for target in operands.targets[1:]]
    if len(upstream) != 1 or upstream[0] is None:
        return False
    branch = git_probe(call, session_cwd, "symbolic-ref", "--short", "-q", "HEAD")
    ref = git_probe(call, session_cwd, "rev-parse", "--symbolic-full-name", upstream[0])
    return bool(branch and ref and ref.startswith("refs/remotes/") and ref.endswith(f"/{branch}"))


def short_force(flag: str) -> bool:
    """Whether a bundled short-flag token such as ``-uf`` carries ``-f`` before any ``-o`` value."""
    for letter in flag[1:]:
        if letter == "o":
            return False
        if letter == "f":
            return True
    return False


def force_pushes(call: Call) -> bool:
    """Whether a ``git push`` overwrites remote history: a force flag or a ``+``-prefixed refspec."""
    for flag in call.flags:
        name = flag.split("=", 1)[0]
        if name in {"--force", "--force-with-lease", "--force-if-includes"}:
            return True
        if name.startswith("-") and not name.startswith("--") and short_force(name):
            return True
    return any((target.value or "").startswith("+") for target in call.targets.targets[1:])


def in_graphite_repo(command: str, queue: str | None = None, **fields: Any) -> Input:
    """An inline-test input that runs ``command`` from a home directory whose ``.git`` points at a Graphite git dir."""
    return Input(
        command=f"cd ~ && {command}",
        file=FileFixture(name=".git", content=f"gitdir: {FIXTURE_GITDIR}\n", home=True),
        commands={"ccx vcs lane": '{"lane": "gt"}', "git config --get ccx.queue": queue if queue else (1, "")},
        **fields,
    )
