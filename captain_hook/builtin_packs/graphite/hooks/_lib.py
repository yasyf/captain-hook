from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import BaseHookEvent, CustomCommandLineCondition, CustomCondition
from captain_hook.util import reqenv
from captain_hook.util.vcs import graphite_lane, graphite_lane_of_git_dir

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

    from captain_hook.cmd import Call

REVIEW_SKILL_PREFIX = "cc-review"
RAW_MARKER = re.compile(r"#\s*ccx:raw\b")
RAW_ENV = "CAPT_HOOK_CCX_RAW"

JJ_READS = frozenset(
    {
        ("log",),
        ("status",),
        ("st",),
        ("show",),
        ("diff",),
        ("evolog",),
        ("interdiff",),
        ("files",),
        ("root",),
        ("version",),
        ("help",),
        ("bookmark", "list"),
        ("tag", "list"),
        ("op", "log"),
        ("op", "show"),
        ("op", "diff"),
        ("operation", "log"),
        ("operation", "show"),
        ("operation", "diff"),
        ("file", "show"),
        ("file", "list"),
        ("config", "list"),
        ("config", "get"),
        ("config", "path"),
        ("workspace", "list"),
        ("workspace", "root"),
        ("sparse", "list"),
    }
)


def is_review_skill(skill: str) -> bool:
    return skill.startswith(REVIEW_SKILL_PREFIX) or skill.split(":", 1)[-1].startswith(REVIEW_SKILL_PREFIX)


def jj_read(call: Call) -> bool:
    verbs = tuple(target.value for target in call.targets)
    return not {"-h", "--help", "-V", "--version"}.isdisjoint(call.flags) or any(
        verbs[: len(read)] == read for read in JJ_READS
    )


def git_location(call: Call, session_cwd: Path | None) -> tuple[Path | None, Path | None]:
    cwd, git_dir = call.cwd or session_cwd, None
    options = iter(call.leading_options)
    for token in options:
        if token == "-C":
            hop = next(options, None)
        elif token.startswith("-C"):
            hop = token[2:]
        elif token == "--git-dir":
            git_dir = next(options, None)
            continue
        elif token.startswith("--git-dir="):
            git_dir = token.removeprefix("--git-dir=")
            continue
        else:
            continue
        if hop is not None:
            path = Path(os.path.expanduser(hop))
            cwd = path if path.is_absolute() else (cwd / path if cwd else None)
    if git_dir is None:
        return cwd, None
    path = Path(os.path.expanduser(git_dir))
    return cwd, path if path.is_absolute() else (cwd / path if cwd else None)


def graphite_owns(call: Call, session_cwd: Path | None) -> bool:
    cwd, git_dir = git_location(call, session_cwd)
    if git_dir is not None:
        return graphite_lane_of_git_dir(git_dir)
    return cwd is not None and graphite_lane(cwd)


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


class JJReads(CustomCommandLineCondition):
    """Matches when every ``jj`` call on the line is a read.

    Every call, not any: ``skip_if`` is an any() over its conditions, so a per-call match
    would let ``jj log && jj new`` skip the block the mutation earns. A verb outside the
    read set — a new one included — leaves the line blocked.
    """

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return bool(jj := evt.cmd.calls("jj")) and all(jj_read(call) for call in jj)


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


class ReviewPassRan(CustomCondition):
    """Matches when a cc-review skill ran this session."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(is_review_skill(skill) for window in evt.ctx.transcript.deep_inputs() for skill in window.skills)


class CcxInstalled(CustomCondition):
    """Matches when ``ccx`` is on PATH, so its stack verbs are there to route raw gt and git writes to."""

    def check(self, evt: BaseHookEvent) -> bool:
        return shutil.which("ccx") is not None


class RawRequested(CustomCommandLineCondition):
    """Matches a command that opts out of ccx steering: a ``# ccx:raw`` comment on the line, or
    ``CAPT_HOOK_CCX_RAW`` set for the session. Either runs the raw command as written."""

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return RAW_MARKER.search(evt.cmd.raw) is not None or bool(reqenv.getenv(RAW_ENV))


def git_probe(call: Call, session_cwd: Path | None, *args: str) -> str | None:
    """Run a read-only ``git`` query against the repository ``call`` targets; ``None`` when it fails."""
    cwd, git_dir = git_location(call, session_cwd)
    if cwd is None:
        return None
    location = ["--git-dir", str(git_dir)] if git_dir is not None else []
    try:
        probe = subprocess.run(
            ["git", "-C", str(cwd), *location, *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
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
