from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.app import on
from captain_hook.state import caller_file, hook_name
from captain_hook.types import Event, InlineTests, TCondition

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

STDERR_TAIL = 2000


def install_binary(
    script: str | Path,
    *,
    label: str | None = None,
    timeout: float = 600,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    tests: InlineTests | None = None,
) -> None:
    """Register a ``SessionStart`` hook that runs a shell *script* to provision a binary.

    *script* resolves relative to the directory of the pack file that calls
    ``install_binary`` — from ``hooks/session.py`` a consumer writes
    ``install_binary("../scripts/install-binary.sh")`` to reach a sibling ``scripts/`` dir.
    The registered hook fires ``async`` on every ``SessionStart`` (resume, clear, and compact
    included), runs ``/bin/sh <script>`` with the script's own directory as ``cwd``, and logs
    the outcome — ``INFO`` on a clean exit, ``WARNING`` (with a stderr tail) on a non-zero exit,
    a spawn failure, or a timeout. The handler never raises and never returns a verdict:
    idempotency and staleness are the script's job, so there is no already-installed fast path.

    Warning:
        Do not pass ``tests=`` unless the inline test only asserts registration shape. An
        ``Input`` fixture that fires this hook executes the real ``script`` in every consumer
        repo that loads the pack — inline tests run on ``capt-hook test`` everywhere.

    Example:
        >>> install_binary("../scripts/install-binary.sh", label="mytool")
    """
    label = label or Path(script).name
    resolved = (Path(caller_file()).parent / script).resolve()
    name = hook_name("install_binary", label, str(resolved))

    def handler(evt: BaseHookEvent) -> None:
        try:
            proc = subprocess.run(
                ["/bin/sh", str(resolved)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                cwd=str(resolved.parent),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.bind(label=label, script=str(resolved)).warning(f"install_binary {label}: run failed: {exc}")
            return None
        if proc.returncode == 0:
            logger.bind(label=label, script=str(resolved)).info(f"install_binary {label}: ok")
        else:
            tail = (proc.stderr or "").strip()[-STDERR_TAIL:]
            logger.bind(label=label, script=str(resolved)).warning(
                f"install_binary {label}: exit {proc.returncode}: {tail}"
            )
        return None

    handler.__name__ = handler.__qualname__ = name
    on(Event.SessionStart, only_if=only_if, skip_if=skip_if, tests=tests, async_=True)(handler)
