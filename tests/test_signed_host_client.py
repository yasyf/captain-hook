from __future__ import annotations

import importlib.metadata
import os
import sys
from typing import Never

import pytest

from capt_hook_client import client


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
