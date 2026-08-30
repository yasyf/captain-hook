"""The ``capt-hook helper`` command group: formula convergence, ping, and notify.

Every side effect (brew or the bridge) is stubbed.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from captain_hook.cli import cli
from captain_hook.helper import FORMULA, client
from captain_hook.helper.client import Lane, NotifyOutcome
from captain_hook.update import updater

CELLAR = "/opt/homebrew/opt/captain-hook"
INSTALL = ["brew", "install", "--formula", FORMULA]
REINSTALL = ["brew", "reinstall", "--formula", FORMULA]
LIST = ["brew", "list", "--versions", FORMULA]
PREFIX = ["brew", "--prefix", FORMULA]
PACKAGE_INSTALL = [f"{CELLAR}/{updater.CELLAR_HOST}", "package-install"]


def invoke(*args: str):  # noqa: ANN201 - click Result
    return CliRunner().invoke(cli, ["helper", *args])


def record_brew(monkeypatch: pytest.MonkeyPatch, *, cellar: str, install_rc: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[1] == "list":
            return SimpleNamespace(returncode=0, stdout=f"captain-hook {cellar}\n", stderr="")
        if argv[1] == "--prefix":
            return SimpleNamespace(returncode=0, stdout=f"{CELLAR}\n", stderr="")
        return SimpleNamespace(returncode=install_rc if argv[1] == "install" else 0, stdout="", stderr="brew error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def stub_host(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    def send(_: object) -> dict[str, object]:
        if version is None:
            raise OSError("no such bridge")
        return {"ok": True, "version": version}

    monkeypatch.setattr(client, "send", send)


def test_helper_group_lists_subcommands() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("install", "status", "notify"):
        assert command in result.output


def test_install_converges_formula_without_launching_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_brew(monkeypatch, cellar="12.22.4")
    stub_host(monkeypatch, "12.22.4")
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert calls == [INSTALL, LIST, PREFIX, PACKAGE_INSTALL]
    assert "Captain Hook 12.22.4 installed" in result.output
    assert "System Settings" in result.output and "widget" in result.output.lower()


def test_install_repairs_existing_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_brew(monkeypatch, cellar="12.22.4", install_rc=1)
    stub_host(monkeypatch, "12.22.4")
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert calls == [INSTALL, REINSTALL, LIST, PREFIX, PACKAGE_INSTALL]


def test_install_fails_loudly_when_the_host_stays_behind_the_cellar(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN: brew exits non-zero on every host today, and exit 0 covers a no-op that deployed nothing.

    Only the host's own ping proves a lane landed, so a Cellar ahead of the running helper is a
    failure that names both versions — never the unconditional success this command used to print.
    """
    record_brew(monkeypatch, cellar="12.22.4")
    stub_host(monkeypatch, "12.21.6")
    result = invoke("install")
    assert result.exit_code != 0
    assert "12.22.4" in result.output and "12.21.6" in result.output
    assert "System Settings" not in result.output


def test_install_fails_loudly_when_the_host_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    record_brew(monkeypatch, cellar="12.22.4")
    stub_host(monkeypatch, None)
    result = invoke("install")
    assert result.exit_code != 0
    assert "12.22.4" in result.output and "unreachable" in result.output


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


def test_install_and_the_updater_deploy_through_one_helper() -> None:
    """PIN: the formula's ``post_install`` is gone, so nothing but ``deploy`` lands the app.

    Homebrew's post-install sandbox denies the ``~/Library/LaunchAgents`` write
    ``package-install`` makes, which failed every ``brew`` lane on every host. Both callers run
    it outside that sandbox, and they must not drift into two copies of that step.
    """
    from captain_hook.helper import cli as helper_cli

    assert helper_cli.deploy is updater.deploy


def test_install_lands_the_cellar_before_reading_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_brew(monkeypatch, cellar="12.22.4")
    stub_host(monkeypatch, "12.22.4")

    assert invoke("install").exit_code == 0
    assert PACKAGE_INSTALL in calls
    assert calls.index(PACKAGE_INSTALL) > calls.index(INSTALL)
