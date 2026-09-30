from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

COMMAND_KEY = re.compile(r"cmd|command|script|shell|exec|args|argv|run|code", re.ASCII | re.IGNORECASE)
MAX_SCAN_DEPTH = 12


def payload_leaves(items: list[object], depth: int) -> Iterator[object]:
    for item in items:
        match item:
            case list() if depth > 0:
                yield from payload_leaves(item, depth - 1)
            case list():
                pass
            case _:
                yield item


def list_leaf_texts(items: list[object], depth: int) -> Iterator[str]:
    leaves = list(payload_leaves(items, depth))
    if leaves and all(isinstance(leaf, str | int | float) for leaf in leaves):
        argv = [str(leaf) for leaf in leaves]
        yield from dict.fromkeys((shlex.join(argv), " ".join(argv)))
    else:
        yield from (leaf for leaf in leaves if isinstance(leaf, str))


def command_texts(value: object, depth: int = MAX_SCAN_DEPTH) -> Iterator[str]:
    match value:
        case dict() as mapping:
            for key, val in mapping.items():
                match val:
                    case str() if COMMAND_KEY.fullmatch(key):
                        yield val
                    case list() if COMMAND_KEY.fullmatch(key):
                        yield from list_leaf_texts(val, depth)
                        if depth > 0:
                            yield from command_texts(val, depth - 1)
                    case dict() | list() if depth > 0:
                        yield from command_texts(val, depth - 1)
            for program in (val for key, val in mapping.items() if isinstance(val, str) and COMMAND_KEY.fullmatch(key)):
                for args in (
                    val
                    for key, val in mapping.items()
                    if isinstance(val, list) and re.fullmatch(r"args|argv", key, re.ASCII | re.IGNORECASE)
                ):
                    yield from list_leaf_texts([program, *args], depth)
        case list() as items if depth > 0:
            for item in items:
                if isinstance(item, dict | list):
                    yield from command_texts(item, depth - 1)
