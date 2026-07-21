from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT_STUB = Path(__file__).resolve().parents[1] / "captain_hook" / "__init__.pyi"

CONSUMER_SURFACE = {
    "Action",
    "Allow",
    "BaseHookEvent",
    "Block",
    "Event",
    "Excerpts",
    "HookContext",
    "HookResult",
    "Input",
    "Rewrite",
    "Tool",
    "TouchedFile",
    "TranscriptFixture",
    "Warn",
    "WorkflowScriptSource",
    "block_command",
    "excerpt_around",
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

LEGACY_SURFACE = ("Transcript", "ToolUse", "ToolUseQuery", "EditInput", "BashInput", "EditOp", "ToolResult")

PROBE = """
import importlib, json, sys
module = importlib.import_module("captain_hook")
assert "pygments" not in sys.modules
assert "hatchling" not in sys.modules
print(json.dumps([name for name in json.loads(sys.argv[1]) if not hasattr(module, name)]))
"""

PACK_IMPORT_PROBE = """
import sys

import captain_hook
from captain_hook.loader import discover_pack
from captain_hook.packs import manager

for name in manager.builtin_names():
    discover_pack(name, manager.resolve_builtin(name).path)

print(",".join(name for name in ("wn", "spacy") if name in sys.modules))
"""

CLI_IMPORT_PROBE = """
import sys

import captain_hook.cli

assert "aiosqlite" not in sys.modules
assert "captain_hook.review.store" not in sys.modules
"""


def exported_names() -> set[str]:
    return {
        alias.asname or alias.name
        for node in ast.parse(ROOT_STUB.read_text()).body
        if isinstance(node, ast.ImportFrom)
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


def test_builtin_pack_discovery_does_not_load_nlp_models() -> None:
    out = subprocess.run([sys.executable, "-c", PACK_IMPORT_PROBE], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_cli_import_does_not_load_review_store() -> None:
    out = subprocess.run([sys.executable, "-c", CLI_IMPORT_PROBE], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
