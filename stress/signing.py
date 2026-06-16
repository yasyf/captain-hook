"""Stabilize the ad-hoc signature of the interpreter the harness execs in a loop.

uv-managed CPython ships ``adhoc, linker-signed`` with ``Identifier=-``. On macOS
26+ (Tahoe), every ``exec`` of a linker-signed binary makes ``syspolicyd``
re-walk the Apple trust anchors; the harness spawns ``capt-hook`` and detached
``review spawn`` children hundreds of times per run, so that re-assessment storm
can pin the daemon and wedge the machine. Re-signing the interpreter once with a
stable ad-hoc identity (``codesign --force --sign -``) drops the ``linker-signed``
flag and gives it a stable cdhash that ``syspolicyd`` caches, so subsequent execs
cost nothing. Idempotent: a no-op once the binary is stably signed, and a no-op
off Darwin.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STABLE_IDENTIFIER = "capt-hook-stress-python"


def is_linker_signed(binary: Path) -> bool:
    proc = subprocess.run(["codesign", "-dv", str(binary)], capture_output=True, text=True)
    return "linker-signed" in proc.stderr


def stabilize(binary: Path) -> bool:
    if not is_linker_signed(binary):
        return False
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--identifier", STABLE_IDENTIFIER, str(binary)],
        capture_output=True,
        check=True,
    )
    return True


def ensure_stable_signatures() -> list[str]:
    """Re-signs the harness's exec'd interpreter on Tahoe; returns what it stabilized."""
    if sys.platform != "darwin":
        return []
    interpreter = Path(sys.executable).resolve()
    return [str(interpreter)] if stabilize(interpreter) else []
