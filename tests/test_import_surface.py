from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT_INIT = Path(__file__).resolve().parents[1] / "captain_hook" / "__init__.py"

CONSUMER_SURFACE = {
    "Action",
    "Allow",
    "BaseHookEvent",
    "Block",
    "Event",
    "HookContext",
    "HookResult",
    "Input",
    "Rewrite",
    "Tool",
    "TouchedFile",
    "TranscriptFixture",
    "Warn",
    "block_command",
    "gate",
    "hook",
    "lint",
    "llm_gate",
    "llm_nudge",
    "nudge",
    "on",
    "prompt_check",
    "rewrite_command",
    "warn_command",
    "workflow",
}

LEGACY_SURFACE = ("Transcript", "Turn", "ToolUse", "ToolUseQuery", "EditInput", "BashInput", "EditOp", "ToolResult")

PROBE = """
import importlib, json, sys
module = importlib.import_module("captain_hook")
print(json.dumps([name for name in json.loads(sys.argv[1]) if not hasattr(module, name)]))
"""


def exported_names() -> set[str]:
    return {
        alias.asname or alias.name
        for node in ast.parse(ROOT_INIT.read_text()).body
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
        for alias in node.names
    }


def resolve_in_subprocess(names: set[str]) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(sorted(names))],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_every_root_export_resolves() -> None:
    names = exported_names()
    assert CONSUMER_SURFACE <= names
    assert resolve_in_subprocess(names) == []


def test_legacy_transcript_surface_is_gone() -> None:
    assert not exported_names() & set(LEGACY_SURFACE)
    assert resolve_in_subprocess(set(LEGACY_SURFACE)) == sorted(LEGACY_SURFACE)
