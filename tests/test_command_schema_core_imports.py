from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["captain_hook.command_schema_core", "captain_hook.command_schemas"])
def test_command_schema_core_import_boundary(module: str, tmp_path: Path) -> None:
    script = """
import importlib
import sys

importlib.import_module(sys.argv[1])

forbidden = (
    "captain_hook.app",
    "captain_hook.command_schema",
    "captain_hook.conditions",
    "captain_hook.state",
    "captain_hook.context",
    "captain_hook.session",
    "captain_hook.snapshots",
    "captain_hook.types",
    "pydantic",
    "pydantic_core",
)
assert not {
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
}

from captain_hook import command_schema
from captain_hook import command_schema_core

for name in (
    "Arguments", "CommandSchema", "Operand", "Option", "OptionIs",
    "PathMatches", "PathsMatch", "Scalar", "Target", "Targets",
    "glob_prefix", "short_cluster",
):
    assert getattr(command_schema, name) is getattr(command_schema_core, name)
assert command_schema.CommandMatches.__bases__ == (command_schema.CustomCondition,)
assert command_schema.CommandMatches.valid_events is command_schema.TOOL_EVENTS
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, module],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
