from __future__ import annotations

import os
from pathlib import Path

SCRATCH_DIR_NAMES = frozenset({"tmp", "temp", "scratch", "scratchpad", "scratchpads"})
# Roots resolve at import (macOS /var -> /private/var). No $TMPDIR: gettempdir() freezes per
# daemon worker, so a custom TMPDIR could pin a writable non-scratch dir as auto-approved.
TEMP_ROOTS = tuple(
    {Path(root).resolve() for root in ("/tmp", "/private/tmp", "/var/folders", "/dev/shm", f"/run/user/{os.getuid()}")}
)


def is_scratch_path(resolved: Path) -> bool:
    """True when an already-resolved absolute path sits under a temp root or a scratch-named ancestor dir."""
    return any(resolved.is_relative_to(root) for root in TEMP_ROOTS) or not SCRATCH_DIR_NAMES.isdisjoint(
        resolved.parts[:-1]
    )
