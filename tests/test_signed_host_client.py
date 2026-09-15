from __future__ import annotations

import os
import sys
from typing import Never

import pytest

from capt_hook_client import client


def capture_exec(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    captured: list[object] = []

    def execv(path: str, argv: list[str]) -> None:
        captured.extend((path, argv))
        raise RuntimeError("exec")

    monkeypatch.setattr(os, "execv", execv)
    return captured


def test_run_execs_the_plain_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", "run", "PreToolUse"])
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/session/project")
    captured = capture_exec(monkeypatch)
    with pytest.raises(RuntimeError, match="exec"):
        client.main()
    assert captured == [client.CLIENT, [client.CLIENT, "run", "PreToolUse"]]
    assert os.environ["CLAUDE_PROJECT_DIR"] == "/session/project"


def test_spelled_root_becomes_the_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", "--root", "/spelled/root", "run", "Stop"])
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/session/project")
    captured = capture_exec(monkeypatch)
    with pytest.raises(RuntimeError, match="exec"):
        client.main()
    assert captured == [client.CLIENT, [client.CLIENT, "run", "Stop"]]
    assert os.environ["CLAUDE_PROJECT_DIR"] == "/spelled/root"


def test_async_twin_exits_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", "run", "PreToolUse", "--async"])
    captured = capture_exec(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        client.main()
    assert excinfo.value.code == 0
    assert captured == []


@pytest.mark.parametrize(
    "argv",
    [[], ["ping"], ["review", "run"], ["run"], ["--hooks", "/tmp/hooks", "run", "Stop"]],
)
def test_unknown_or_obsolete_client_grammar_fails(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", *argv])
    with pytest.raises(SystemExit) as excinfo:
        client.main()
    assert excinfo.value.code == 1  # a usage/grammar failure is infrastructure, not a hook verdict (exit 2)


def test_ops_execs_fixed_host_without_python_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hookd", "status"])
    captured = capture_exec(monkeypatch)
    with pytest.raises(RuntimeError, match="exec"):
        client.ops_main()
    assert captured == [client.HOST, [client.HOST, "status"]]


def test_missing_client_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", "run", "Stop"])

    def missing_execv(_path: str, _argv: list[str]) -> Never:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(os, "execv", missing_execv)
    with pytest.raises(SystemExit, match="1"):
        client.main()
