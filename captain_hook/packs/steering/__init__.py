"""Importable building blocks for the steering nudges.

Re-exports from the skip-marked ``lib`` module only, never from ``steering`` —
``from captain_hook.packs.steering import TypeCheckerContext, pre_existing_nudge``
pulls in the inert library and registers nothing. Enabling the pack (``[packs.steering]``)
is what auto-loads ``steering.py`` and registers the two nudges.
"""

from __future__ import annotations

from captain_hook.packs.steering.lib import (
    PRE_EXISTING_SIGNALS,
    TRIVIAL_TYPE_SIGNALS,
    TypeCheckerContext,
    pre_existing_nudge,
    trivial_type_nudge,
)

__all__ = [
    "PRE_EXISTING_SIGNALS",
    "TRIVIAL_TYPE_SIGNALS",
    "TypeCheckerContext",
    "pre_existing_nudge",
    "trivial_type_nudge",
]
