"""The async self-updater for the signed Captain Hook host."""

from __future__ import annotations

from captain_hook.update.settings import UpdateSettings
from captain_hook.update.updater import dispatch_update, run_update

__all__ = ["UpdateSettings", "dispatch_update", "run_update"]
