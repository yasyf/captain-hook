"""Shared pack-contract identity: the captain-hook marketplace/plugin slugs, the plugin.json
dependency version-floor pattern, and the upward path reader that ``pack lint`` and ``pack scaffold``
share. Imported by ``cli``, ``packs.scaffold``, and ``packs.bootstrap`` alike; it imports nothing from
any of them, so the shared constants live here without a cycle."""

from __future__ import annotations

import re
from pathlib import Path

DIST_NAME = "capt-hook"
DEFAULT_PREFIX = f"uvx --isolated {DIST_NAME}"
PLUGIN_ID = "captain-hook@captain-hook"
MARKETPLACE_NAME = "captain-hook"
MARKETPLACE_REPO = "yasyf/captain-hook"

# A version floor is a lower-bound constraint: `>=X.Y.Z` (a bare pin or `<=`/`==` doesn't let a
# newer captain-hook resolve).
VERSION_FLOOR_RE = re.compile(r">=\s*\d+\.\d+\.\d+")


def search_upward(start: Path, *rel: str, stop: Path | None = None) -> Path | None:
    """The nearest existing ``base/rel`` walking from ``start`` upward; when ``stop`` is given the
    walk halts at ``stop`` (inclusive) and never ascends above it."""
    bound = stop.resolve() if stop is not None else None
    for base in (start, *start.parents):
        for r in rel:
            if (cand := base / r).is_file():
                return cand
        if bound is not None and base.resolve() == bound:
            break
    return None
