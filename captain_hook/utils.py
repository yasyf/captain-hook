from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast, overload


def resolve_binary(name: str, *, extra_dirs: Sequence[Path] = ()) -> str | None:
    """Resolve an absolute, executable path for *name*, or None.

    Search order: ``$CLAUDE_PLUGIN_ROOT/bin/<name>``, then each *extra_dirs* entry's
    ``<name>``, then ``shutil.which(name)``.
    """
    roots = [Path(root) / "bin" for root in (os.environ.get("CLAUDE_PLUGIN_ROOT"),) if root]
    return next(
        (
            str(candidate)
            for d in (*roots, *extra_dirs)
            if (candidate := d / name).is_file() and os.access(candidate, os.X_OK)
        ),
        shutil.which(name),
    )


def kebab(name: str) -> str:
    """Convert a TitleCamelCase name to kebab-case (``NoNestedImports`` -> ``no-nested-imports``)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


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
