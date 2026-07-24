"""Settings for the async self-updater."""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from captain_hook.settings import HooksSettings


class UpdateSettings(HooksSettings):
    """Self-updater settings, backed by environment variables with ``HOOKS_UPDATE_`` prefix.

    ``enabled`` gates the whole SessionStart check, and ``interval_minutes`` is the minimum
    time between two release checks — throttled with the shared claim stamp so a burst of
    sessions triggers at most one ``brew upgrade`` per window.
    """

    model_config = SettingsConfigDict(env_prefix="HOOKS_UPDATE_")

    enabled: bool = True
    interval_minutes: int = 720
