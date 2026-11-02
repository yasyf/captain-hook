from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import Allow, Annotated, BaseHookEvent, Block, Event, HookResult, Input, Tool, on
from captain_hook.cmd import git_location

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.cmd import Call

BRANCH_FLAGS = frozenset({"-b", "-B", "--orphan"})
SHOWN_FILES = 3


@dataclass(frozen=True, slots=True)
class Discard:
    pathspecs: tuple[str, ...]
    worktree: bool
    staged: bool


def short_letters(call: Call) -> frozenset[str]:
    return frozenset(letter for flag in call.flags if flag[:1] == "-" and flag[1:2] != "-" for letter in flag[1:])


def discard_of(call: Call) -> Discard | None:
    if call.name != "git":
        return None
    verb, *args = call.verb_argv[1:] or ("",)
    flags = frozenset(call.flags)
    if verb == "reset":
        return Discard((), True, True) if "--hard" in flags else None
    if verb not in {"checkout", "restore"} or not BRANCH_FLAGS.isdisjoint(flags):
        return None
    treeish: list[str] = []
    if "--" in args:
        split = args.index("--")
        treeish, pathspecs = args[:split], args[split + 1 :]
    else:
        pathspecs = [arg for arg in args if not arg.startswith("-")]
    if not pathspecs:
        return None
    if verb == "checkout":
        return Discard(tuple(pathspecs), True, bool(treeish))
    staged = "--staged" in flags or "S" in short_letters(call)
    worktree = "--worktree" in flags or "W" in short_letters(call) or not staged
    return Discard(tuple(pathspecs), worktree, staged)


def modified_files(call: Call, session_cwd: Path | None, discard: Discard) -> list[str]:
    cwd, git_dir = git_location(call, session_cwd)
    if cwd is None:
        return []
    location = ["--git-dir", str(git_dir)] if git_dir is not None else []
    pathspecs = ["--", *discard.pathspecs] if discard.pathspecs else []
    probe = subprocess.run(
        ["git", *location, "status", "--porcelain", "-z", "--untracked-files=no", *pathspecs],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    if probe.returncode != 0:
        return []
    files: list[str] = []
    entries = iter(probe.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        index, worktree, path = entry[0], entry[1], entry[3:]
        if index in "RC":
            next(entries, None)
        if (discard.worktree and worktree != " ") or (discard.staged and index != " "):
            files.append(path)
    return files


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash")],
    skip_if=[Annotated("discard-ok"), Annotated("raw")],
    tests={
        Input(command="git checkout src/a.rs", cwd="/", commands={"git status": " M src/a.rs\0"}): Block(
            pattern="src/a.rs.*ccx:discard-ok"
        ),
        Input(command="git checkout -- src/a.rs", cwd="/", commands={"git status": " M src/a.rs\0"}): Block(),
        Input(command="git restore src/a.rs", cwd="/", commands={"git status": " M src/a.rs\0"}): Block(),
        Input(command="git reset --hard", cwd="/", commands={"git status": "M  src/a.rs\0 M b.rs\0"}): Block(
            pattern="src/a.rs, b.rs"
        ),
        Input(command="git checkout HEAD -- src/a.rs", cwd="/", commands={"git status": "M  src/a.rs\0"}): Block(),
        Input(command="git checkout -b feature", cwd="/", commands={"git status": " M src/a.rs\0"}): Allow(),
        Input(command="git checkout main", cwd="/", commands={"git status": ""}): Allow(),
        Input(command="git checkout src/a.rs", cwd="/", commands={"git status": ""}): Allow(),
        Input(command="git checkout src/a.rs", cwd="/", commands={"git status": "M  src/a.rs\0"}): Allow(),
        Input(command="git restore --staged src/a.rs", cwd="/", commands={"git status": " M src/a.rs\0"}): Allow(),
        Input(command="git reset --soft HEAD~1", cwd="/", commands={"git status": " M src/a.rs\0"}): Allow(),
        Input(command="git reset --hard", cwd="/", commands={"git status": ""}): Allow(),
        Input(
            command="git checkout src/a.rs  # ccx:discard-ok", cwd="/", commands={"git status": " M src/a.rs\0"}
        ): Allow(),
        Input(command="git status", cwd="/", commands={"git status": " M src/a.rs\0"}): Allow(),
    },
)
def edits_survive_checkout(evt: BaseHookEvent) -> HookResult | None:
    files: list[str] = []
    for call in evt.cmd.calls("git"):
        if (discard := discard_of(call)) is not None:
            files.extend(modified_files(call, evt.cwd, discard))
    if not files:
        return None
    names = list(dict.fromkeys(files))
    shown = ", ".join(names[:SHOWN_FILES]) + (
        f" and {len(names) - SHOWN_FILES} more" if len(names) > SHOWN_FILES else ""
    )
    return evt.block(
        f"This discards uncommitted edits to {shown}. "
        "Commit a WIP checkpoint first, or end the command with `# ccx:discard-ok`."
    )
