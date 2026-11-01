from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script",
    [
        """
import sys
import captain_hook.results

assert not {
    "captain_hook.dispatch",
    "captain_hook.cli",
    "captain_hook.tickets.declare",
    "captain_hook.session",
    "captain_hook.snapshots.client",
    "captain_hook.loader",
}.intersection(sys.modules)
""",
        """
import socket
import subprocess
import sys
from pathlib import Path

def unexpected(*args: object, **kwargs: object) -> None:
    raise AssertionError("search guard invoked unrelated external work")

subprocess.Popen = unexpected
socket.socket.connect = unexpected

import captain_hook.loader as loader
loader.discover_hooks = unexpected
loader.discover_pack = unexpected

from captain_hook.context import HookContext
from captain_hook.snapshots import client

client.client_scope = unexpected
client.SnapshotClient.__init__ = unexpected
original = HookContext.__getattribute__

def no_transcript(self: HookContext, name: str) -> object:
    if name in {"transcript", "root_transcript", "t", "turn", "prior"}:
        raise AssertionError("search guard accessed transcript evidence")
    return original(self, name)

HookContext.__getattribute__ = no_transcript
import captain_hook.search_guard as guard
root = Path(sys.argv[1])
raw = {
    "session_id": "native-import-test",
    "turn_id": "native-import-turn",
    "tool_use_id": "native-import-tool",
    "model": "gpt-6.1-sol",
    "permission_mode": "bypassPermissions",
    "hook_event_name": "PreToolUse",
    "cwd": str(root),
    "transcript_path": str(root / "must-not-open.jsonl"),
    "tool_name": "Bash",
    "tool_input": {"command": "printf safe"},
}
assert guard.check(raw) is None
denied = guard.check(raw | {"tool_input": {"command": "rg needle ."}})
assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
assert not {
    "captain_hook.dispatch",
    "captain_hook.cli",
    "captain_hook.tickets.declare",
}.intersection(sys.modules)
""",
        """
import sys
import captain_hook.dispatch as dispatch
import captain_hook.results as results

assert "captain_hook.tickets.declare" in sys.modules
from captain_hook.tickets.declare import lifted
assert dispatch.lifted is lifted
for name in (
    "run_declarative", "closed_verdict", "collect_budget", "settled",
    "format_permission_decision", "format_output", "combine", "envelope_text",
):
    assert getattr(dispatch, name) is getattr(results, name)
""",
        """
import importlib.abc
import sys

class RejectTicketInitialization(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "captain_hook.tickets.declare":
            raise ImportError("ticket initialization failed")
        return None

sys.meta_path.insert(0, RejectTicketInitialization())
try:
    import captain_hook.dispatch
except ImportError as exc:
    assert str(exc) == "ticket initialization failed"
else:
    raise AssertionError("ticket initialization was deferred past dispatch import")
""",
    ],
    ids=["results-only", "search-only", "eager-tickets", "ticket-import-error"],
)
def test_search_guard_import_boundaries(script: str, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
