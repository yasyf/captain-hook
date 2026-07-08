"""Structural proof that a Bash command line only reads — never mutates — state.

Backs the public ``ReadOnlyCommand`` condition: a line passes only when it carries no
shell substitution anywhere in its raw text and every segment is a bare, allowlisted
read-only program with safe redirects, no environment prefix, and no privilege wrapper.
Anything it cannot prove read-only fails closed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_transcript.command import Command, CommandLine, Redirect

SUBSTITUTION = re.compile(r"`|\$\(|<\(|>\(")
EXECUTABLE = re.compile(r"[A-Za-z0-9][\w.+-]*")
SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})

READ_ONLY_COMMANDS: dict[str, tuple[str, ...]] = {
    "ls": (),
    "cat": (),
    "head": (),
    "tail": (),
    "wc": (),
    "pwd": (),
    "echo": (),
    "printf": (),
    "grep": (),
    "true": (),
    "false": (),
    "test": (),
    "file": (),
    "stat": (),
    "readlink": (),
    "realpath": (),
    "basename": (),
    "dirname": (),
    "du": (),
    "df": (),
    "which": (),
    "type": (),
    "uname": (),
    "id": (),
    "whoami": (),
    "hostname": (),
    "groups": (),
    "ps": (),
    "pgrep": (),
    "cut": (),
    "tr": (),
    "column": (),
    "nl": (),
    "jq": (),
    "diff": (),
    "cmp": (),
    "comm": (),
    "md5": (),
    "md5sum": (),
    "shasum": (),
    "sha256sum": (),
    "cksum": (),
    "rg": (r"^--pre(=|$)", r"^--hostname-bin(=|$)"),
    "fd": (r"^-[A-Za-z]*[xX]", r"^--exec"),
    "find": (r"^-delete$", r"^-exec", r"^-ok", r"^-fls$", r"^-fprint"),
    "sort": (r"^-o$", r"^--output(=|$)"),
    "tree": (r"^-o$",),
    "date": (r"^-s$", r"^--set(=|$)"),
}

GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "shortlog",
        "describe",
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-tree",
        "cat-file",
        "grep",
        "count-objects",
        "merge-base",
        "name-rev",
        "check-ignore",
        "cherry",
    }
)
GIT_LEADING_BARE = frozenset({"--no-pager", "-P"})
GIT_LEADING_VALUED = frozenset({"-C"})
GIT_FORBIDDEN = (r"^--output(=|$)", r"^-o$", r"^-O", r"^--open-files-in-pager$", r"^--ext-diff$", r"^--textconv$")

JJ_SUBCOMMANDS = frozenset({"log", "status", "st", "diff", "show"})
JJ_LEADING_BARE = frozenset({"--no-pager", "--ignore-working-copy"})
JJ_LEADING_VALUED = frozenset({"-R", "--repository", "--at-operation", "--at-op", "--color"})


def safe_redirect(redirect: Redirect) -> bool:
    match redirect.op:
        case "<" | "<<":
            return True
        case ">&" | "<&":
            return redirect.target.isdigit() or redirect.target == "-"
        case ">" | ">>":
            return redirect.target in SAFE_REDIRECT_TARGETS
        case _:
            return False


def read_only_subcommand(
    args: tuple[str, ...],
    bare: frozenset[str],
    valued: frozenset[str],
    subcommands: frozenset[str],
    forbidden: tuple[str, ...],
) -> bool:
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in bare:
            i += 1
        elif tok in valued:
            i += 2
        elif tok.startswith("-"):
            return False
        else:
            return tok in subcommands and not any(re.search(p, a) for p in forbidden for a in args[i + 1 :])
    return False


def read_only_program(name: str, cmd: Command) -> bool:
    match name:
        case "git":
            return read_only_subcommand(cmd.args, GIT_LEADING_BARE, GIT_LEADING_VALUED, GIT_SUBCOMMANDS, GIT_FORBIDDEN)
        case "jj":
            return read_only_subcommand(cmd.args, JJ_LEADING_BARE, JJ_LEADING_VALUED, JJ_SUBCOMMANDS, ())
        case "uniq":
            return sum(not a.startswith("-") for a in cmd.args) <= 1
        case _ if (forbidden := READ_ONLY_COMMANDS.get(name)) is not None:
            return not cmd.has_arg(*forbidden)
        case _:
            return False


def read_only_command(cmd: Command) -> bool:
    if cmd.env or any(not safe_redirect(r) for r in cmd.redirects):
        return False
    unwrapped = cmd.unwrapped
    if any(tok in ("sudo", "doas") or "=" in tok for tok in cmd.argv[: len(cmd.argv) - len(unwrapped.argv)]):
        return False
    if not EXECUTABLE.fullmatch(unwrapped.executable):
        return False
    return read_only_program(unwrapped.executable, unwrapped)


def is_read_only(cl: CommandLine) -> bool:
    if not cl or SUBSTITUTION.search(cl.raw):
        return False
    return all(read_only_command(c) for c in cl.commands)
