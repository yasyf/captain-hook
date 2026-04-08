# Example: Settings and configuration via conf.py
#
# Demonstrates how to define project-level settings using
# `HooksSettings` and how to use `build_settings` to construct
# a typed, environment-variable-backed configuration object.

from captain_hook import HooksSettings, build_settings

import types


class ProjectSettings(HooksSettings):
    test_command: str = "pytest"
    max_retries: int = 3
    enforce_style: bool = True
    excluded_dirs: list[str] = ["vendor", "node_modules"]


conf_module = types.ModuleType("conf")
conf_module.ProjectSettings = ProjectSettings  # type: ignore[attr-defined]

settings = build_settings(conf_module)

assert isinstance(settings, ProjectSettings)
assert settings.test_command == "pytest"
assert settings.max_retries == 3
assert settings.enforce_style is True
assert settings.excluded_dirs == ["vendor", "node_modules"]

auto_module = types.ModuleType("auto_conf")
auto_module.greeting = "hello"  # type: ignore[attr-defined]
auto_module.retry_count = 5  # type: ignore[attr-defined]
auto_module.verbose = False  # type: ignore[attr-defined]

auto_settings = build_settings(auto_module)
assert auto_settings.greeting == "hello"  # type: ignore[union-attr]
assert auto_settings.retry_count == 5  # type: ignore[union-attr]
assert auto_settings.verbose is False  # type: ignore[union-attr]

print("✓ Explicit and auto-inferred settings built correctly")
