from __future__ import annotations

import re
from pathlib import Path


def unescape_shell(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw)


def normalize_executable(raw: str) -> str:
    return Path(
        unescape_shell(raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"" else raw)
    ).name.casefold()
