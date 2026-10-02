from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from spawnllm import TModel

from captain_hook.resource_defaults import DEFAULTS, ENV_PREFIX
from captain_hook.settings import HooksSettings

MAX_SECONDS = 86_400


class PerformanceSettings(HooksSettings):
    """Resource-monitor settings, backed by environment variables with the ``HOOKS_PERFORMANCE_`` prefix.

    The sampling, threshold, grace, and escalation knobs default from ``internal/wireproto/resource.json``,
    the contract capt-hookd reads too. ``terminate`` turns the stop after a disposable verdict on or off,
    and the ``judge_*`` knobs bound the small-model disposability verdict. ``escalate_after_seconds=0`` turns
    the ``SIGKILL`` escalation off; every other duration is positive and at most a day.
    """

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    enabled: bool = Field(default=DEFAULTS["enabled"])
    sample_interval_seconds: int = Field(default=DEFAULTS["sample_interval_seconds"], gt=0, le=MAX_SECONDS)
    min_runtime_seconds: int = Field(default=DEFAULTS["min_runtime_seconds"], gt=0, le=MAX_SECONDS)
    cpu_fraction: float = Field(default=DEFAULTS["cpu_fraction"], gt=0, le=1)
    sustain_seconds: int = Field(default=DEFAULTS["sustain_seconds"], gt=0, le=MAX_SECONDS)
    disk_bytes_per_second: int = Field(default=DEFAULTS["disk_bytes_per_second"], ge=0)
    grace_seconds: int = Field(default=DEFAULTS["grace_seconds"], gt=0, le=MAX_SECONDS)
    escalate_after_seconds: int = Field(default=DEFAULTS["escalate_after_seconds"], ge=0, le=MAX_SECONDS)
    max_tracked_per_session: int = Field(default=DEFAULTS["max_tracked_per_session"], ge=1)
    registry_cap: int = Field(default=DEFAULTS["registry_cap"], ge=1)
    terminate: bool = True
    judge_tier: TModel = "small"
    judge_timeout_seconds: int = Field(default=20, ge=1, le=300)
    max_judge_calls_per_session: int = Field(default=10, ge=1)
