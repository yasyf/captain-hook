from __future__ import annotations

import json
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
@pytest.mark.parametrize("explicit_cwd", [False, True], ids=["default-cwd", "explicit-cwd"])
def test_inflight_cli_child_is_reaped_on_its_request_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: bool, throw: bool, explicit_cwd: bool
) -> None:
    database, pid_path = tmp_path / "blocked.sqlite", tmp_path / "child.pid"
    command_cwd = tmp_path / "command"
    command_cwd.mkdir()
    context_path = tmp_path / "child.context"
    monkeypatch.setattr("captain_hook.context.resolve_project_dir", lambda: tmp_path)
    request = reqenv.RequestOverrides(env=dict(os.environ) | {"CAPT_HOOK_CWD_PROBE": "owned"}, cwd=str(tmp_path), client_ppid=os.getpid(), session_id="cli-lifetime")
    cutoff = reqenv.Cutoff()
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    errors: list[BaseException] = []
    code = "import json,os,pathlib,sqlite3,sys;pathlib.Path(sys.argv[2]).write_text(str(os.getpid()));ready=pathlib.Path(sys.argv[3]);pending=ready.with_suffix('.tmp');pending.write_text(json.dumps([os.getcwd(),os.environ['CAPT_HOOK_CWD_PROBE']]));pending.replace(ready);sqlite3.connect(sys.argv[1],timeout=10).execute('BEGIN IMMEDIATE')"
    argv = [sys.executable, "-c", code, str(database), str(pid_path), str(context_path)]

    def call() -> None:
        try:
            with reqenv.use_request(request), reqenv.abandonable(cutoff):
                ctx.call_cli(argv, timeout=10, throw=throw, cwd=str(command_cwd) if explicit_cwd else None)
        except BaseException as error:
            errors.append(error)

    writer = sqlite3.connect(database)
    writer.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=call)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while not context_path.exists():
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
        assert json.loads(context_path.read_text()) == [str(command_cwd if explicit_cwd else tmp_path), "owned"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        writer.rollback()
        writer.close()
        worker.join(12)
    assert not worker.is_alive()


@pytest.mark.parametrize("selection", ["project", "request", "explicit"])
def test_cli_cwd_selects_the_explicit_path_or_the_existing_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    project, requested, explicit = (tmp_path / name for name in ("project", "request", "explicit"))
    for path in (project, requested, explicit):
        path.mkdir()
    monkeypatch.setattr("captain_hook.context.resolve_project_dir", lambda: None if selection == "request" else project)
    request = reqenv.RequestOverrides(env={"CAPT_HOOK_CWD_PROBE": "request"}, cwd=str(requested), client_ppid=os.getpid(), session_id="cli-cwd")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    argv = [sys.executable, "-c", "import json,os;print(json.dumps([os.getcwd(),os.environ['CAPT_HOOK_CWD_PROBE']]))"]
    with reqenv.use_request(request):
        output = ctx.call_cli(argv, cwd=str(explicit) if selection == "explicit" else None)
    assert output is not None
    expected = {"project": project, "request": requested, "explicit": explicit}[selection]
    assert json.loads(output) == [str(expected), "request"]


def test_inline_command_stubs_retain_explicit_cwd_environment_and_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from captain_hook.app import on, reset
    from captain_hook.testing.helpers import run_inline_tests
    from captain_hook.testing.types import Allow, Input, Warn
    from captain_hook.types import Event
    from spawnllm import proc

    observed = []
    real_cli = proc.run_cli

    def record(args, **kwargs):
        observed.append((args, kwargs["cwd"], kwargs["env"].get("CAPT_HOOK_CWD_PROBE"), kwargs["checkpoint"]))
        return real_cli(args, **kwargs)

    monkeypatch.setattr(proc, "run_cli", record)
    reset()

    @on(Event.Stop, tests={
        Input(commands={"fixture-query list": "answer"}, env={"CAPT_HOOK_CWD_PROBE": "success"}): Warn(pattern="answer"),
        Input(commands={"fixture-query": (3, "failure")}, env={"CAPT_HOOK_CWD_PROBE": "error"}): Allow(),
    })
    def query(evt):
        if output := evt.ctx.call_cli(["fixture-query", "list"], cwd=str(tmp_path), throw=False):
            return evt.warn(output)
        return None

    results = run_inline_tests()
    assert len(results) == 2 and all(result[2] for result in results), results
    assert observed == [
        (["fixture-query", "list"], str(tmp_path), "success", None),
        (["fixture-query", "list"], str(tmp_path), "error", None),
    ]


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
