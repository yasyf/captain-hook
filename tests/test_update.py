"""The async self-updater: release check, throttled dispatch, and formula repair.

Every test mocks only the boundaries — the GitHub release fetch, the signed-bridge ping that
reports the installed version, ``brew``, and the notification seam — and leaves
:mod:`captain_hook.update.updater` real. The autouse ``clean_state`` fixture isolates the on-disk
state dir, so the throttle stamp never leaks across tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from captain_hook.desktop import client
from captain_hook.desktop.client import Lane, NotifyOutcome
from captain_hook.update import updater


@pytest.fixture(autouse=True)
def homebrew_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture(autouse=True)
def alias_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "physical", lambda candidate: candidate)


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def record(**kwargs: object) -> NotifyOutcome:
        calls.append(kwargs)
        return NotifyOutcome(Lane.bridge, ok=True, error=None)

    monkeypatch.setattr(client, "notify", record)
    return calls


def stub_release(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    monkeypatch.setattr(updater, "github_get_json", lambda url: {"tag_name": tag})


def stub_installed(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(client, "send", lambda *a, **k: {"ok": True, "version": version})


def stub_converging(monkeypatch: pytest.MonkeyPatch, before: str, after: str, *, on: str) -> dict[str, str]:
    """Report ``before`` until the ``on`` brew lane runs, then ``after`` — a landed supersede."""
    host = {"version": before}
    monkeypatch.setattr(client, "send", lambda *a, **k: {"ok": True, "version": host["version"]})
    original = updater.brew

    def converging(args: list[str]) -> bool:
        outcome = original(args)
        if args[0] == on:
            host["version"] = after
        return outcome

    monkeypatch.setattr(updater, "brew", converging)
    return host


def record_brew(
    monkeypatch: pytest.MonkeyPatch, returncode: Any, *, candidate: str | None = None, kegs: tuple[str, ...] = ()
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> SimpleNamespace:
        calls.append(argv)
        if argv == VERSION and candidate is not None:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"schema": 1, "build": candidate}) + "\n", stderr="")
        if argv == LIST and kegs:
            return SimpleNamespace(returncode=0, stdout=f"captain-hook {' '.join(kegs)}\n", stderr="")
        prefix = argv[:2] == PREFIX[:2]
        rc = 0 if prefix else returncode(argv) if callable(returncode) else returncode
        return SimpleNamespace(returncode=rc, stdout=CELLAR if prefix else "", stderr="brew error")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    return calls


CELLAR = "/opt/homebrew/opt/captain-hook"
REAL_PHYSICAL = updater.physical
UPGRADE = ["brew", "upgrade", "--formula", updater.FORMULA]
REINSTALL = ["brew", "reinstall", "--formula", updater.FORMULA]
INSTALL = ["brew", "install", "--formula", updater.FORMULA]
LIST = ["brew", "list", "--versions", updater.FORMULA]
PREFIX = ["brew", "--prefix", updater.FORMULA]
PACKAGE_INSTALL = [f"{CELLAR}/{updater.CELLAR_HOST}", "package-install"]
VERSION = [f"{CELLAR}/{updater.CELLAR_HOST}", "version"]
ESCALATED_WITHOUT_INSTALL = [UPGRADE, PREFIX, VERSION, REINSTALL, PREFIX, VERSION, INSTALL, PREFIX, VERSION]


def lanes(calls: list[list[str]]) -> list[list[str]]:
    """Only the brew install lanes; deploy's prefix lookup and package-install are its own."""
    return [call for call in calls if call[0] == "brew" and call[1] != "--prefix"]


@pytest.mark.parametrize(
    ("installed", "latest", "older"),
    [
        ("1.0.0", "v2.0.0", True),
        ("v1.2.3", "1.2.3", False),
        ("1.2.10", "1.2.9", False),
        ("1.2.9", "1.2.10", True),
    ],
)
def test_version_tuple_ordering(installed: str, latest: str, older: bool) -> None:
    assert (updater.version_tuple(installed) < updater.version_tuple(latest)) is older


def test_run_update_upgrades_when_host_is_older(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    brew = record_brew(monkeypatch, 0)
    stub_converging(monkeypatch, "1.0.0", "2.0.0", on="upgrade")

    updater.run_update()

    assert lanes(brew) == [UPGRADE]
    assert notes == [
        {"kind": "update_installed", "title": "Captain Hook updated", "body": "Upgraded the signed host to 2.0.0."}
    ]


def test_run_update_settles_without_package_install_once_the_host_reaches_the_release(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v22.0.0")
    calls = record_brew(monkeypatch, 0, candidate="22.0.0", kegs=("18.0.0", "19.0.0", "21.0.0", "22.0.0"))
    stub_converging(monkeypatch, "21.0.0", "22.0.0", on="upgrade")

    updater.run_update()

    assert calls == [UPGRADE]
    assert notes == [
        {"kind": "update_installed", "title": "Captain Hook updated", "body": "Upgraded the signed host to 22.0.0."}
    ]


@pytest.mark.parametrize("host", ["22.0.0", "21.0.0"], ids=["host-equals-candidate", "host-older-than-candidate"])
def test_run_update_never_package_installs_a_candidate_short_of_the_release(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]], host: str
) -> None:
    stub_release(monkeypatch, "v23.0.0")
    stub_installed(monkeypatch, host)
    calls = record_brew(monkeypatch, 0, candidate="22.0.0")

    updater.run_update()

    assert calls == ESCALATED_WITHOUT_INSTALL
    assert [n["kind"] for n in notes] == ["update_failed"]
    assert updater.escalations("v23.0.0") == 1


@pytest.mark.parametrize(
    ("returncode", "candidate"),
    [
        pytest.param(0, None, id="empty-reply"),
        pytest.param(lambda argv: 1 if argv == VERSION else 0, None, id="version-exits-nonzero"),
        pytest.param(0, "2.0.0-rc.1", id="unparseable-build"),
    ],
)
def test_run_update_never_package_installs_a_candidate_whose_version_is_unread(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]], returncode: Any, candidate: str | None
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    calls = record_brew(monkeypatch, returncode, candidate=candidate)

    updater.run_update()

    assert calls == ESCALATED_WITHOUT_INSTALL
    assert [n["kind"] for n in notes] == ["update_failed"]


def test_run_update_package_installs_the_candidate_that_reaches_the_release(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")

    def landing(argv: list[str]) -> int:
        if argv == PACKAGE_INSTALL:
            stub_installed(monkeypatch, "2.0.0")
        return 0

    calls = record_brew(monkeypatch, landing, candidate="2.0.0")

    updater.run_update()

    assert calls == [UPGRADE, PREFIX, VERSION, PACKAGE_INSTALL]
    assert notes == [
        {"kind": "update_installed", "title": "Captain Hook updated", "body": "Upgraded the signed host to 2.0.0."}
    ]


def test_run_update_versions_and_installs_the_keg_it_resolved_while_the_alias_repoints(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]], tmp_path: Path
) -> None:
    kegs = {version: tmp_path / "Cellar" / version for version in ("22.0.0", "23.0.0")}
    for keg in kegs.values():
        (keg / updater.CELLAR_HOST).parent.mkdir(parents=True)
        (keg / updater.CELLAR_HOST).touch()
    alias = tmp_path / "opt"
    alias.symlink_to(kegs["23.0.0"])
    pinned = str((kegs["23.0.0"] / updater.CELLAR_HOST).resolve())
    monkeypatch.setattr(updater, "physical", REAL_PHYSICAL)
    stub_release(monkeypatch, "v23.0.0")
    stub_installed(monkeypatch, "22.0.0")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> SimpleNamespace:
        calls.append(argv)
        match argv:
            case [_, "--prefix", _]:
                return SimpleNamespace(returncode=0, stdout=f"{alias}\n", stderr="")
            case [_, "version"]:
                alias.unlink()
                alias.symlink_to(kegs["22.0.0"])
                return SimpleNamespace(returncode=0, stdout=json.dumps({"schema": 1, "build": "23.0.0"}), stderr="")
            case [_, "package-install"]:
                stub_installed(monkeypatch, "23.0.0")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    updater.run_update()

    assert calls == [UPGRADE, PREFIX, [pinned, "version"], [pinned, "package-install"]]
    assert [n["kind"] for n in notes] == ["update_installed"]


def test_run_update_escalates_when_upgrade_exits_clean_without_converging(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    """PIN: a Cellar already carrying the release makes ``brew upgrade`` a successful no-op.

    The deployed app stays behind, so exit 0 must not be read as an upgrade — the escalation
    is what fills a Cellar brew left short, and :func:`~captain_hook.update.updater.deploy` is
    what supersedes the deployment from it.
    """
    stub_release(monkeypatch, "v2.0.0")
    brew = record_brew(monkeypatch, 0)
    stub_converging(monkeypatch, "1.0.0", "2.0.0", on="reinstall")

    updater.run_update()

    assert lanes(brew) == [UPGRADE, REINSTALL]
    assert [n["kind"] for n in notes] == ["update_installed"]


def test_run_update_reports_a_failure_when_nothing_converges(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 0)

    updater.run_update()

    assert lanes(brew) == [UPGRADE, REINSTALL, INSTALL]
    assert [n["kind"] for n in notes] == ["update_failed"]


def test_run_update_stops_escalating_after_the_budget(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 0)

    for _ in range(updater.MAX_ESCALATIONS + 2):
        updater.run_update()

    assert lanes(brew).count(REINSTALL) == updater.MAX_ESCALATIONS
    assert lanes(brew).count(UPGRADE) == updater.MAX_ESCALATIONS
    assert [n["kind"] for n in notes] == ["update_failed"] * updater.MAX_ESCALATIONS


def test_run_update_runs_no_package_install_once_the_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    """PIN: an exhausted tag does no work at all — not even the upgrade + deploy lane.

    ``deploy`` quiesces and supersedes the running daemon, so a release that cannot converge
    must not take hook dispatch down on every throttle window forever; the budget gates the
    whole apply lane, not just the reinstall escalation.
    """
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    record_brew(monkeypatch, 0)
    for _ in range(updater.MAX_ESCALATIONS):
        updater.run_update()

    calls = record_brew(monkeypatch, 0)
    updater.run_update()

    assert calls == []
    assert [n["kind"] for n in notes] == ["update_failed"] * updater.MAX_ESCALATIONS


def test_run_update_gives_a_newer_release_a_fresh_budget(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    record_brew(monkeypatch, 0)
    for _ in range(updater.MAX_ESCALATIONS):
        updater.run_update()

    stub_release(monkeypatch, "v3.0.0")
    brew = record_brew(monkeypatch, 0)
    stub_converging(monkeypatch, "1.0.0", "3.0.0", on="reinstall")
    updater.run_update()

    assert lanes(brew) == [UPGRADE, REINSTALL]
    assert notes[-1]["kind"] == "update_installed"


def test_run_update_skips_when_host_is_current(monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]) -> None:
    stub_release(monkeypatch, "v1.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 0)

    updater.run_update()

    assert lanes(brew) == []
    assert notes == []


def test_run_update_reinstalls_and_recovers_husk(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    brew = record_brew(monkeypatch, lambda argv: 1 if argv[1] == "upgrade" else 0)
    stub_converging(monkeypatch, "1.0.0", "2.0.0", on="reinstall")

    updater.run_update()

    assert lanes(brew) == [UPGRADE, REINSTALL]
    assert [n["kind"] for n in notes] == ["update_installed"]


def test_run_update_notifies_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 1)

    updater.run_update()

    assert lanes(brew) == [UPGRADE, REINSTALL, INSTALL]
    assert notes == [
        {
            "kind": "update_failed",
            "title": "Captain Hook update failed",
            "body": "Could not upgrade the host to v2.0.0.",
        }
    ]


def test_run_update_skips_when_installed_version_unavailable(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")

    def boom(*_a: Any, **_k: Any) -> dict[str, object]:
        raise OSError("bridge unreachable")

    monkeypatch.setattr(client, "send", boom)
    brew = record_brew(monkeypatch, 0)

    updater.run_update()

    assert lanes(brew) == []
    assert notes == []


def record_detach(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    detaches: list[bool] = []
    monkeypatch.setattr(updater, "detach", lambda *, apply: detaches.append(apply))
    return detaches


def test_dispatch_detaches_once_per_throttle_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    detaches = record_detach(monkeypatch)

    updater.dispatch_update()
    updater.dispatch_update()

    assert detaches == [True]


def test_dispatch_skips_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    detaches = record_detach(monkeypatch)

    updater.dispatch_update()

    assert detaches == []


def test_dispatch_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    monkeypatch.setenv("HOOKS_UPDATE_ENABLED", "false")
    detaches = record_detach(monkeypatch)

    updater.dispatch_update()

    assert detaches == []


def test_dispatch_skips_a_spawned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
    detaches = record_detach(monkeypatch)

    updater.dispatch_update()

    assert detaches == []


def test_dispatch_checks_an_agent_session_without_letting_it_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN: a headless session participates in the check and can never supersede the daemon.

    Converging quiesces and replaces the running host, so an agent-launched session acting on it
    would drain hook dispatch under every other session the daemon serves.
    """
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    detaches = record_detach(monkeypatch)

    updater.dispatch_update()

    assert detaches == [False]
    assert updater.update_argv(*updater.run_args(apply=False))[-1] == "--check-only"


def test_the_check_only_flag_reaches_run_update(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from captain_hook.update.cli import update

    applied: list[bool] = []
    monkeypatch.setattr(updater, "run_update", lambda *, apply: applied.append(apply))

    assert CliRunner().invoke(update, ["run", "--check-only"]).exit_code == 0
    assert CliRunner().invoke(update, ["run"]).exit_code == 0
    assert applied == [False, True]


def test_check_only_records_the_divergence_and_runs_no_brew_lane(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 0)

    updater.run_update(apply=False)

    assert lanes(brew) == []
    assert notes == []
    assert updater.pending() == "v2.0.0"


def test_a_deferred_update_is_applied_by_the_next_interactive_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    record_brew(monkeypatch, 0)
    detaches = record_detach(monkeypatch)

    updater.dispatch_update()
    updater.run_update(apply=False)
    assert updater.pending() == "v2.0.0"

    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    updater.dispatch_update()
    updater.dispatch_update()

    assert detaches == [False, True]


def test_a_converged_host_drops_the_deferral(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    record_brew(monkeypatch, 0)
    updater.run_update(apply=False)
    assert updater.pending() == "v2.0.0"

    stub_installed(monkeypatch, "2.0.0")
    updater.run_update(apply=False)

    assert updater.pending() is None


def test_run_update_deploys_the_cellar_after_every_brew_lane(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    calls = record_brew(monkeypatch, 0, candidate="2.0.0")

    updater.run_update()

    assert calls == [
        UPGRADE,
        PREFIX,
        VERSION,
        PACKAGE_INSTALL,
        REINSTALL,
        PREFIX,
        VERSION,
        PACKAGE_INSTALL,
        INSTALL,
        PREFIX,
        VERSION,
        PACKAGE_INSTALL,
    ]
    assert [n["kind"] for n in notes] == ["update_failed"]
