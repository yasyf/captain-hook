from __future__ import annotations

import inspect
import os
import re
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import FrameType

FRAMEWORK_DIR = str(Path(__file__).resolve().parent.parent)
BUILTIN_PACKS_DIR = str(Path(FRAMEWORK_DIR) / "builtin_packs")
PACK_PACKAGE_PREFIX = "captain_hook._packs"


def package_aware_stem(p: Path) -> str:
    if str(p).startswith(BUILTIN_PACKS_DIR):
        return f"{p.parent.parent.name}.{p.stem}"
    if (
        p.name != "__init__.py"
        and not str(p).startswith(FRAMEWORK_DIR)
        and (init := p.parent / "__init__.py").exists()
        and init.stat().st_size > 0
    ):
        return f"{p.parent.name}.{p.stem}"
    return p.stem


@lru_cache(maxsize=4096)
def framework_path(absolute: str) -> bool:
    resolved = Path(absolute).resolve()
    return resolved.is_relative_to(FRAMEWORK_DIR) and not resolved.is_relative_to(BUILTIN_PACKS_DIR)


def framework_frame(filename: str) -> bool:
    return framework_path(os.path.join(os.getcwd(), filename))


def caller_frame() -> FrameType | None:
    frame: FrameType | None = inspect.currentframe()
    if frame:
        frame = frame.f_back
    while frame and framework_frame(frame.f_code.co_filename):
        frame = frame.f_back
    return frame


def caller_stem() -> str:
    if (frame := caller_frame()) is None:
        return "unknown"
    if (name := frame.f_globals.get("__name__", "")).startswith(f"{PACK_PACKAGE_PREFIX}."):
        return name.removeprefix(f"{PACK_PACKAGE_PREFIX}.")
    return package_aware_stem(Path(frame.f_code.co_filename))


def caller_file() -> str:
    return frame.f_code.co_filename if (frame := caller_frame()) else ""


def hook_name(prefix: str, label: str | None, message: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") if label else sha256(message.encode()).hexdigest()[:8]
    return f"{caller_stem()}:{prefix}_{suffix}"
