from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent


def read_source(evt: BaseHookEvent) -> str | None:
    if evt.file and evt.file.path.exists():
        try:
            return evt.file.read_text()
        except (OSError, UnicodeDecodeError):
            pass
    return evt.content


def reconstruct_pre(evt: BaseHookEvent, source: str) -> str:
    """Best-effort pre-edit source: an Edit's ``new_string`` swapped back to ``old_string``.

    Returns ``""`` when there is nothing to diff against (a Write, or an Edit whose fragment
    can't be located) — the conservative choice that treats the whole file as changed.
    """
    match (evt.old, evt.content):
        case (str() as old, str() as new) if new and new in source:
            return source.replace(new, old, 1)
        case _:
            return ""


def changed_lines(pre: str, source: str) -> set[int]:
    """1-based line numbers in ``source`` that differ from ``pre``.

    An empty ``pre`` means "everything changed" (a Write or an unlocatable Edit), so every line
    of ``source`` is returned.
    """
    new_lines = source.splitlines()
    if not pre:
        return set(range(1, len(new_lines) + 1))
    return {
        i
        for tag, _, _, lo, hi in difflib.SequenceMatcher(a=pre.splitlines(), b=new_lines, autojunk=False).get_opcodes()
        if tag in {"replace", "insert"}
        for i in range(lo + 1, hi + 1)
    }
