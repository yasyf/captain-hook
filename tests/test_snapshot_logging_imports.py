from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger

from captain_hook.snapshots.client import EvidenceIncomplete, SnapshotClient


def test_app_import_does_not_initialize_snapshot_logging(tmp_path: Path) -> None:
    script = """
import importlib.abc
import sys

class RejectLoggingImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "loguru":
            raise AssertionError("App import initialized snapshot logging")
        return None

sys.meta_path.insert(0, RejectLoggingImport())
import captain_hook.app
assert "loguru" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_snapshot_deadline_log_keeps_its_logger_and_callsite(logcap) -> None:
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="INFO")
    client = SnapshotClient(lambda _: pytest.fail("expired foreground request was sent"), foreground_seconds=0)
    client.bind_tool_registry({})
    try:
        with pytest.raises(EvidenceIncomplete, match="foreground transcript deadline exhausted"):
            client.call("acquire", path="/tmp/root.jsonl", classifier={"id": "native", "version": "1"})
    finally:
        logger.remove(sink)

    assert len(records) == 1
    record = records[0]
    assert record["name"] == "captain_hook.snapshots.client"
    assert record["function"] == "_call"
    assert record["message"] == "foreground transcript deadline exhausted"
    assert record["extra"]["operation"] == "acquire"
    assert record["extra"]["status"] == "deadline"
