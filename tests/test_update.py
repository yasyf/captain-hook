"""The async self-updater: release check, throttled dispatch, and brew upgrade with force-retry.

Every test mocks only the boundaries — the GitHub release fetch, the signed-bridge ping that
reports the installed version, ``brew``, and the notification seam — and leaves
:mod:`captain_hook.update.updater` real. The autouse ``clean_state`` fixture isolates the on-disk
state dir, so the throttle stamp never leaks across tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from captain_hook.helper import client
from captain_hook.helper.client import Lane, NotifyOutcome
from captain_hook.update import updater


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


def record_brew(monkeypatch: pytest.MonkeyPatch, returncode: Any) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> SimpleNamespace:
        calls.append(argv)
        rc = returncode(argv) if callable(returncode) else returncode
        return SimpleNamespace(returncode=rc, stderr="brew error")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    return calls


UPGRADE = ["brew", "upgrade", "--cask", updater.CASK]
FORCE = ["brew", "install", "--cask", "--force", updater.CASK]


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
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 0)

    updater.run_update()

    assert brew == [UPGRADE]
    assert notes == [
        {"kind": "update_installed", "title": "Captain Hook updated", "body": "Upgraded the signed host to v2.0.0."}
    ]


def test_run_update_skips_when_host_is_current(monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]) -> None:
    stub_release(monkeypatch, "v1.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 0)

    updater.run_update()

    assert brew == []
    assert notes == []


def test_run_update_force_retries_and_recovers_husk(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, lambda argv: 1 if argv[1] == "upgrade" else 0)

    updater.run_update()

    assert brew == [UPGRADE, FORCE]
    assert [n["kind"] for n in notes] == ["update_installed"]


def test_run_update_notifies_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch, notes: list[dict[str, object]]
) -> None:
    stub_release(monkeypatch, "v2.0.0")
    stub_installed(monkeypatch, "1.0.0")
    brew = record_brew(monkeypatch, 1)

    updater.run_update()

    assert brew == [UPGRADE, FORCE]
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

    assert brew == []
    assert notes == []


def test_dispatch_detaches_once_per_throttle_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    detaches: list[int] = []
    monkeypatch.setattr(updater, "detach", lambda: detaches.append(1))

    updater.dispatch_update()
    updater.dispatch_update()

    assert detaches == [1]


def test_dispatch_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    monkeypatch.setenv("HOOKS_UPDATE_ENABLED", "false")
    detaches: list[int] = []
    monkeypatch.setattr(updater, "detach", lambda: detaches.append(1))

    updater.dispatch_update()

    assert detaches == []


def test_dispatch_skips_a_spawned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
    detaches: list[int] = []
    monkeypatch.setattr(updater, "detach", lambda: detaches.append(1))

    updater.dispatch_update()

    assert detaches == []
