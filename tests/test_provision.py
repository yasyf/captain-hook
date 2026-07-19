from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import _state
from captain_hook.primitives import provision
from captain_hook.primitives.provision import install_binary
from captain_hook.testing.helpers import mock_session_start_event
from captain_hook.types import Event
from tests.helpers import dispatch_test


def write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    return path


class TestInstallBinary:
    def test_registers_async_session_start_hook(self, tmp_path: Path) -> None:
        install_binary(tmp_path / "install.sh", label="mytool")
        assert len(_state.hooks) == 1
        (entry,) = _state.hooks
        assert entry.spec.events is Event.SessionStart
        assert entry.spec.async_ is True
        assert entry.spec.max_fires is None

    def test_runs_script_and_returns_none(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        script = write_script(tmp_path / "install.sh", f"echo installed > {marker}")
        install_binary(script, label="mytool")
        result = dispatch_test("SessionStart", async_=True)
        assert result is None
        assert marker.read_text().strip() == "installed"

    def test_runs_script_from_script_parent(self, tmp_path: Path) -> None:
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        script = write_script(script_dir / "install.sh", "echo installed > marker")
        install_binary(script, label="mytool")

        assert dispatch_test("SessionStart", async_=True) is None
        assert (script_dir / "marker").read_text().strip() == "installed"

    @pytest.mark.parametrize(
        ("body", "kwargs", "warning"),
        [
            pytest.param("echo boom >&2\nexit 7", {}, "exit 7: boom", id="nonzero_exit"),
            pytest.param(None, {}, "No such file", id="missing_script"),
            pytest.param("sleep 10", {"timeout": 0.5}, "run failed", id="timeout"),
            pytest.param("printf '\\377' >&2\nexit 7", {}, "exit 7: \ufffd", id="invalid_utf8"),
        ],
    )
    def test_failure_modes_return_none_without_raising(
        self,
        tmp_path: Path,
        body: str | None,
        kwargs: dict[str, float],
        warning: str,
        logcap: Any,
    ) -> None:
        script = tmp_path / "install.sh"
        if body is not None:
            write_script(script, body)
        install_binary(script, label="mytool", **kwargs)
        (entry,) = _state.hooks
        assert entry.handler is not None

        assert entry.handler(mock_session_start_event()) is None
        assert any(record.levelno >= logging.WARNING and warning in record.message for record in logcap.records)

    def test_resolves_script_relative_to_caller_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pack file at hooks/session.py reaches a sibling scripts/ dir via a relative path.
        pack_file = tmp_path / "hooks" / "session.py"
        pack_file.parent.mkdir()
        pack_file.write_text("")
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        marker = tmp_path / "marker"
        write_script(scripts_dir / "install-binary.sh", f"echo resolved > {marker}")

        monkeypatch.setattr(provision, "caller_file", lambda: str(pack_file))
        install_binary("../scripts/install-binary.sh")

        assert dispatch_test("SessionStart", async_=True) is None
        assert marker.read_text().strip() == "resolved"
