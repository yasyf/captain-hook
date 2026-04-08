# Example: Settings and configuration via conf.py
#
# Demonstrates how to define project-level settings using
# `HooksSettings` and how `build_settings` constructs a typed,
# environment-variable-backed configuration object.
#
# In practice, settings are loaded from a conf.py module
# discovered alongside your hooks directory.

from captain_hook import HooksSettings


class ProjectSettings(HooksSettings):
    """Project-specific hook configuration."""

    test_command: str = "pytest"
    max_retries: int = 3
    enforce_style: bool = True
    excluded_dirs: list[str] = ["vendor", "node_modules"]


# Access settings via HookContext.settings (or ctx.conf / ctx.c)
# at dispatch time.  The settings object merges defaults from the
# class with overrides from environment variables.
