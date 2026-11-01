"""The ``capt-hook helper`` command group: formula convergence, ping, and notify.

Every side effect (brew or the bridge) is stubbed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from captain_hook.cli import cli
from captain_hook.desktop import FORMULA, client
from captain_hook.desktop.client import Lane, NotifyOutcome
from captain_hook.update import updater

CELLAR = "/opt/homebrew/opt/captain-hook"
INSTALL = ["brew", "install", "--formula", FORMULA]
REINSTALL = ["brew", "reinstall", "--formula", FORMULA]
PREFIX = ["brew", "--prefix", FORMULA]
PACKAGE_INSTALL = [f"{CELLAR}/{updater.CELLAR_HOST}", "package-install"]
VERSION = [f"{CELLAR}/{updater.CELLAR_HOST}", "version"]
RELEASE = "v12.22.4"


@pytest.fixture(autouse=True)
def latest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "github_get_json", lambda url: {"tag_name": RELEASE})
    monkeypatch.setattr(updater, "physical", lambda candidate: candidate)


def invoke(*args: str):  # noqa: ANN201 - click Result
    return CliRunner().invoke(cli, ["helper", *args])


def stub_host(monkeypatch: pytest.MonkeyPatch, version: str | None) -> dict[str, str | None]:
    host: dict[str, str | None] = {"version": version}

    def send(_: object) -> dict[str, object]:
        if host["version"] is None:
            raise OSError("no such bridge")
        return {"ok": True, "version": host["version"]}

    monkeypatch.setattr(client, "send", send)
    return host


def record_brew(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cellar: str,
    install_rc: int = 0,
    host: dict[str, str | None] | None = None,
) -> list[list[str]]:
    """Fake brew and the Cellar's host; a ``package-install`` moves ``host`` to the Cellar's version."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> SimpleNamespace:
        calls.append(argv)
        if argv == PACKAGE_INSTALL and host is not None:
            host["version"] = cellar
        if argv == VERSION:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"schema": 1, "build": cellar}), stderr="")
        if argv[1] == "list":
            return SimpleNamespace(returncode=0, stdout=f"captain-hook {cellar}\n", stderr="")
        if argv[1] == "--prefix":
            return SimpleNamespace(returncode=0, stdout=f"{CELLAR}\n", stderr="")
        return SimpleNamespace(returncode=install_rc if argv[1] == "install" else 0, stdout="", stderr="brew error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_helper_group_lists_subcommands() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("install", "status", "notify"):
        assert command in result.output


def test_install_converges_formula_without_launching_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_brew(monkeypatch, cellar="12.22.4", host=stub_host(monkeypatch, "12.21.6"))
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert calls == [INSTALL, PREFIX, VERSION, PACKAGE_INSTALL]
    assert "Captain Hook 12.22.4 installed" in result.output
    assert "System Settings" in result.output and "widget" in result.output.lower()


def test_install_repairs_existing_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_brew(monkeypatch, cellar="12.22.4", install_rc=1, host=stub_host(monkeypatch, "12.21.6"))
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert calls == [INSTALL, REINSTALL, PREFIX, VERSION, PACKAGE_INSTALL]


def test_install_keeps_a_host_already_at_the_latest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN: a hand-installed host ahead of the Cellar is never package-installed over with the older Cellar."""
    calls = record_brew(monkeypatch, cellar="12.21.6")
    stub_host(monkeypatch, "12.22.4")
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert "Captain Hook 12.22.4 installed" in result.output
    assert PACKAGE_INSTALL not in calls


def test_install_fails_while_the_cellar_is_short_of_the_latest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN: a Cellar behind the latest release is a failure naming all three versions, never "installed"."""
    calls = record_brew(monkeypatch, cellar="12.22.1")
    stub_host(monkeypatch, "12.21.6")
    result = invoke("install")
    assert result.exit_code != 0
    assert f"reports 12.21.6 and Homebrew holds 12.22.1, short of the latest release {RELEASE}" in result.output
    assert PACKAGE_INSTALL not in calls


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


def test_install_narrates_the_deploy_and_surfaces_why_package_install_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN: ``install`` used to run every step captured and print nothing until it finished.

    ``package-install`` runs for minutes and restarts the host, and its failure reason went only to
    the update log while the error said "did not converge" — a run the owner read as a hang.
    """
    stub_host(monkeypatch, "12.21.6")
    calls = record_brew(monkeypatch, cellar="12.22.4")
    reason = "captain package: install the capt-hook tool env: simulated"
    recorded = subprocess.run

    def failing_package_install(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv != PACKAGE_INSTALL:
            return recorded(argv, **kwargs)
        calls.append(argv)
        if kwargs["capture_output"]:
            return SimpleNamespace(returncode=1, stdout="", stderr=reason)
        print(reason, file=sys.stderr)
        return SimpleNamespace(returncode=1, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", failing_package_install)
    result = invoke("install")
    assert result.exit_code != 0
    assert PACKAGE_INSTALL in calls
    assert result.stderr.index(f"Landing Captain Hook {RELEASE}") < result.stderr.index(reason)
    assert "package-install exit 1" in result.stderr
    assert str(updater.update_log_path()) in result.stderr


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


def test_install_and_the_updater_converge_through_one_helper() -> None:
    """PIN: the formula's ``post_install`` is gone, so nothing but ``converge`` lands the app.

    Homebrew's post-install sandbox denies the ``~/Library/LaunchAgents`` write
    ``package-install`` makes, which failed every ``brew`` lane on every host. Both callers run
    it outside that sandbox, and they must not drift into two copies of that step.
    """
    from captain_hook.desktop import cli as helper_cli

    assert helper_cli.converge is updater.converge


def test_install_lands_the_cellar_before_reading_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_brew(monkeypatch, cellar="12.22.4", host=stub_host(monkeypatch, "12.21.6"))

    assert invoke("install").exit_code == 0
    assert PACKAGE_INSTALL in calls
    assert calls.index(PACKAGE_INSTALL) > calls.index(INSTALL)
