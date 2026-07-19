from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast, overload

from captain_hook.util import reqenv
from captain_hook.util.caching import ttl_cache


def resolve_binary(name: str, *, extra_dirs: Sequence[Path] = ()) -> str | None:
    """Resolve an absolute, executable path for *name*, or None.

    Search order: ``$CLAUDE_PLUGIN_ROOT/bin/<name>``, then each *extra_dirs* entry's
    ``<name>``, then ``shutil.which(name)``.
    """
    roots = [Path(root) / "bin" for root in (reqenv.getenv("CLAUDE_PLUGIN_ROOT"),) if root]
    return next(
        (
            str(candidate)
            for d in (*roots, *extra_dirs)
            if (candidate := d / name).is_file() and os.access(candidate, os.X_OK)
        ),
        shutil.which(name),
    )


@ttl_cache(600)
def binary_supports(name: str, flag: str) -> bool:
    """Whether the binary *name* advertises *flag* in its ``--help`` output.

    Resolves *name* through :func:`resolve_binary`, probes ``<path> --help`` with a 2s timeout,
    and tests *flag* for token membership in the combined stdout/stderr — splitting on runs of
    non-flag characters, so a bracketed ``[--foo]`` or a ``--foo=VALUE`` form still matches. A
    missing binary, a probe error, or a timeout yields ``False``. Memoized for 600s, so a hot path
    pays the probe at most once per window.
    """
    if (path := resolve_binary(name)) is None:
        return False
    try:
        probe = subprocess.run([path, "--help"], capture_output=True, text=True, errors="replace", timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in re.split(r"[^\w-]+", probe.stdout + probe.stderr)


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a unique temp file in the same directory.

    The temp name is per-call (``mkstemp``), so concurrent writers to the same path never
    consume each other's temp file — one writer's ``os.replace`` can't yank another's out.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=f"{path.suffix}.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@overload
def read_json(path: Path) -> dict[str, Any] | None: ...


@overload
def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]: ...


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read and parse a JSON file, returning *default* on missing file or parse error."""
    try:
        return cast("dict[str, Any]", data) if isinstance(data := json.loads(path.read_text()), dict) else default
    except (OSError, json.JSONDecodeError, ValueError):
        return default
