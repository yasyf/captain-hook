from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import stubbed_commands
from captain_hook.util import reqenv


@pytest.mark.parametrize("host", [True, False], ids=["request-abandon", "fanout-cutoff"])
@pytest.mark.parametrize("throw", [True, False], ids=["throw", "nonthrowing"])
def test_inflight_cli_child_is_reaped_on_its_request_signal(tmp_path: Path, host: bool, throw: bool) -> None:
    database, pid_path = tmp_path / "blocked.sqlite", tmp_path / "child.pid"
    request = reqenv.RequestOverrides(env=dict(os.environ), cwd=str(tmp_path), client_ppid=os.getpid(), session_id="cli-lifetime")
    cutoff = reqenv.Cutoff()
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    errors: list[BaseException] = []
    code = "import os,pathlib,sqlite3,sys;pathlib.Path(sys.argv[2]).write_text(str(os.getpid()));sqlite3.connect(sys.argv[1],timeout=10).execute('BEGIN IMMEDIATE')"
    argv = [sys.executable, "-c", code, str(database), str(pid_path)]

    def call() -> None:
        try:
            with reqenv.use_request(request), reqenv.abandonable(cutoff):
                ctx.call_cli(argv, timeout=10, throw=throw)
        except BaseException as error:
            errors.append(error)

    writer = sqlite3.connect(database)
    writer.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=call)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while not pid_path.exists():
            assert time.monotonic() < deadline, "request-owned SQLite child never started"
            time.sleep(0.01)
        pid = int(pid_path.read_text())
        if host:
            request.abandon.set()
        else:
            cutoff.close()
        worker.join(2)
        assert not worker.is_alive(), f"Capt CLI child survived request cancellation: pid={pid}"
        assert len(errors) == 1 and isinstance(errors[0], reqenv.Abandoned)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        writer.rollback()
        writer.close()
        worker.join(12)
    assert not worker.is_alive()


def test_command_fixture_stubs_keep_cli_semantics() -> None:
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    with stubbed_commands({"fixture-cli": "output", "fixture-error": (3, "failure")}):
        assert ctx.call_cli(["fixture-cli"]) == "output"
        assert ctx.call_cli(["fixture-error"], throw=False) is None
        with pytest.raises(subprocess.CalledProcessError) as raised:
            ctx.call_cli(["fixture-error"])
    assert raised.value.returncode == 3
    assert raised.value.stderr == "failure"
    assert "stderr: failure" in raised.value.__notes__
