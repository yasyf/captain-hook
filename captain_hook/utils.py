from __future__ import annotations

import json
from pathlib import Path
from typing import Any, overload


@overload
def read_json(path: Path) -> dict[str, Any] | None: ...


@overload
def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]: ...


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read and parse a JSON file, returning *default* on missing file or parse error."""
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError, ValueError):
        return default
