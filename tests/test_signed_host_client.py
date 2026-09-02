from __future__ import annotations

import importlib.metadata
import os
import sys
from typing import Never

import pytest

from capt_hook_client import client


def test_deleted_cwd_still_spells_a_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN: a workspace deleted under a live session leaves every later hook with no cwd.

    ``os.getcwd()`` raises ``FileNotFoundError`` there, which failed the dispatch before it was
    spelled — observed 2026-09-02 against a deleted ``~/.orca/workspaces`` root, one layer above
    the worker crash fixed in the same release.
    """

    def gone() -> Never:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(sys, "argv", ["hook", "run", "Stop"])
    monkeypatch.setattr(os, "getcwd", gone)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)

    def version(_name: str) -> str:
        return "12.9.1"

    monkeypatch.setattr(importlib.metadata, "version", version)
    captured: list[object] = []

    def execv(path: str, argv: list[str]) -> None:
        captured.extend((path, argv))
        raise RuntimeError("exec")

    monkeypatch.setattr(os, "execv", execv)
    with pytest.raises(RuntimeError, match="exec"):
        client.main()
    argv = captured[1]
    assert isinstance(argv, list)
    assert argv[argv.index("--cwd") + 1] == os.path.sep
    assert argv[argv.index("--root") + 1] == os.path.sep


def test_run_execs_fixed_host_with_exact_product_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", "--root", "/spelled/root", "run", "PreToolUse", "--async"])
    monkeypatch.setattr(os, "getcwd", lambda: "/request/cwd")

    def version(_name: str) -> str:
        return "12.9.1"

    monkeypatch.setattr(importlib.metadata, "version", version)
    captured: list[object] = []

    def execv(path: str, argv: list[str]) -> None:
        captured.extend((path, argv))
        raise RuntimeError("exec")

    monkeypatch.setattr(os, "execv", execv)
    with pytest.raises(RuntimeError, match="exec"):
        client.main()
    assert captured == [
        client.HOST,
        [
            client.HOST,
            "run",
            "--event",
            "PreToolUse",
            "--root",
            "/spelled/root",
            "--cwd",
            "/request/cwd",
            "--python",
            sys.executable,
            "--build",
            "12.9.1",
            "--async",
        ],
    ]


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
    captured: list[object] = []

    def execv(path: str, argv: list[str]) -> None:
        captured.extend((path, argv))
        raise RuntimeError("exec")

    monkeypatch.setattr(os, "execv", execv)
    with pytest.raises(RuntimeError, match="exec"):
        client.ops_main()
    assert captured == [client.HOST, [client.HOST, "status"]]


def test_missing_signed_host_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hook", "run", "Stop"])

    def version(_name: str) -> str:
        return "12.9.1"

    def missing_execv(_path: str, _argv: list[str]) -> Never:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(importlib.metadata, "version", version)
    monkeypatch.setattr(os, "execv", missing_execv)
    with pytest.raises(SystemExit, match="1"):
        client.main()
