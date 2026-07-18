from __future__ import annotations

import glob
import os
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.command import parse_command_line

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

SAFE_WORD = re.compile(r"[^\s'\"\\$`;&|<>(){}#]+")
SHELL_C_FLAG = re.compile(r"-[a-z]*c")
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})
NESTED_COMMAND_DEPTH = 3


def unescape_shell(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


def normalize_executable(raw: str) -> str:
    return Path(
        unescape_shell(raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"" else raw)
    ).name.casefold()


def emit_token(raw: str, *, plain_words: bool) -> str | None:
    if SAFE_WORD.fullmatch(raw):
        if (glob.has_magic(raw) or raw.startswith("~")) and not plain_words:
            return None
        return f"./{raw}" if raw.startswith("-") else raw
    if (
        not any(char in raw for char in "'\"$`{}")
        and ("\\" not in raw or plain_words)
        and not glob.has_magic(target := unescape_shell(raw))
        and not target.startswith("~")
    ):
        return shlex.quote(target)
    return None


def resolve_cd(args: tuple[str, ...], cwd: Path | None) -> Path | None:
    match args:
        case (token,) if "$" not in token and token != "-":
            path = Path(os.path.expanduser(unescape_shell(token)))
            resolved = path.resolve() if path.is_absolute() else (cwd / path).resolve() if cwd is not None else None
            return resolved if resolved is not None and resolved.is_dir() else cwd
        case _:
            return None


def plain_words(raw: str) -> bool:
    return "'" not in raw and '"' not in raw


def nested_command_string(program: str, args: tuple[str, ...]) -> str | None:
    """The command string from a shell ``-c``/combined ``-...c`` cluster or ``eval``, or ``None``.

    ``sh -c '<cmd>'`` / ``bash -euo pipefail -c '<cmd>'`` yields the token after ``-c``;
    ``eval a b c`` yields the args joined. ``args`` is the unwrapped command's arguments.
    """
    if program in SHELLS:
        return next(
            (args[i + 1] for i, arg in enumerate(args) if i + 1 < len(args) and SHELL_C_FLAG.fullmatch(arg)),
            None,
        )
    if program == "eval":
        return " ".join(args) or None
    return None


def safe_parse_command_line(text: str) -> CommandLine | None:
    """Parse ``text``, returning ``None`` when it is too deeply nested to parse.

    Tree-sitter's ``walk_node`` recurses per nesting level, so a pathologically nested string
    (a thousand ``(``…) overflows the Python stack. Untrusted payloads and re-parsed shell
    ``-c`` bodies reach this, so a ``RecursionError`` falls open to "not dangerous" — the safe
    direction for a courtesy approver — rather than crashing dispatch.
    """
    try:
        return parse_command_line(text)
    except RecursionError:
        return None
