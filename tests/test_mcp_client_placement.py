from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from captain_hook import mcp_server


def test_places_the_client_from_the_installed_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = tmp_path / "capt-hookd"
    host.touch()
    monkeypatch.setattr(mcp_server, "INSTALLED_HOST", host)
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    mcp_server.place_client()
    [(argv, kwargs)] = calls
    assert argv == [host, "install-client"]
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_skips_a_machine_without_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "INSTALLED_HOST", tmp_path / "missing")
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    mcp_server.place_client()
    assert calls == []


def test_a_failed_exec_never_aborts_mcp_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = tmp_path / "capt-hookd"
    host.touch()
    monkeypatch.setattr(mcp_server, "INSTALLED_HOST", host)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", refuse)
    mcp_server.place_client()
