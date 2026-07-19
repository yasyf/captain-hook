"""The ``capt-hook helper`` command group: install/launch argv, ping, and the notify test surface.

Every side effect (brew, ``open``, the bridge) is stubbed, so the suite drives the commands
without touching Homebrew, launching the app, or invoking the installed executable.
"""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from captain_hook.cli import cli
from captain_hook.helper import client
from captain_hook.helper.client import Lane, NotifyOutcome


def invoke(*args: str):  # noqa: ANN201 - click Result
    return CliRunner().invoke(cli, ["helper", *args])


def test_helper_group_lists_subcommands() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("install", "status", "notify"):
        assert command in result.output


def test_install_installs_and_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert ["brew", "install", "--cask", "--force", "yasyf/tap/captain-hook"] in calls
    assert ["open", "-g", "/Applications/Captain Hook.app"] in calls
    assert "System Settings" in result.output and "widget" in result.output.lower()


def test_status_pings_and_prints_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "send", lambda _: {"ok": True, "version": "1.0.0"})
    result = invoke("status")
    assert result.exit_code == 0, result.output
    assert "helper v1.0.0 ok=True" in result.output


def test_status_reports_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: object) -> dict[str, object]:
        raise OSError("no such socket")

    monkeypatch.setattr(client, "send", boom)
    result = invoke("status")
    assert result.exit_code != 0
    assert "helper not reachable" in result.output


def test_notify_reports_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def record(**kwargs: object) -> NotifyOutcome:
        captured.update(kwargs)
        return NotifyOutcome(Lane.bridge, ok=True, error=None)

    monkeypatch.setattr(client, "notify", record)
    result = invoke("notify", "--kind", "pr_open", "--title", "T", "--url", "https://x/pull/1")
    assert result.exit_code == 0, result.output
    assert captured["kind"] == "pr_open"
    assert captured["title"] == "T"
    assert captured["url"] == "https://x/pull/1"
    assert "lane=bridge ok=True" in result.output
