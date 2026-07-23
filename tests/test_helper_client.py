"""The Python-to-signed-bridge seam and notification retry loop."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from captain_hook.helper import client
from captain_hook.helper.client import Lane, NotifyOutcome

NOTIFY_FIELDS = {
    "title": "Block force-pushes",
    "kind": "pr_open",
    "subtitle": "captain-hook",
    "body": "Rule guard-rm-rf opened",
    "url": "https://github.com/yasyf/captain-hook/pull/12",
    "repo": "github.com/yasyf/captain-hook",
}


@pytest.fixture
def helper_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bridge = tmp_path / "capt-hook-helper-client"
    monkeypatch.setenv("CAPT_HOOK_HELPER_DIR", str(tmp_path))
    monkeypatch.setenv("CAPT_HOOK_HELPER_CLIENT", str(bridge))
    return tmp_path, bridge


def result(reply: object | None, returncode: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    stdout = b"" if reply is None else json.dumps(reply).encode()
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_paths_honor_overrides(helper_paths: tuple[Path, Path]) -> None:
    directory, bridge = helper_paths
    assert client.helper_dir() == directory
    assert client.bridge_path() == bridge
    assert client.status_path() == directory / "status.json"


def test_ping_invokes_explicit_bridge_path(helper_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _, bridge = helper_paths
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return result({"ok": True, "version": "1.2.3"})

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert client.send("ping") == {"ok": True, "version": "1.2.3"}
    assert calls == [
        (
            [str(bridge), "ping"],
            {"input": None, "capture_output": True, "timeout": client.BRIDGE_TIMEOUT, "check": False},
        )
    ]


def test_notify_passes_typed_json_on_stdin(helper_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(_: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return result({"ok": True})

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = client.notify(**NOTIFY_FIELDS)
    assert outcome == NotifyOutcome(Lane.bridge, ok=True, error=None)
    assert json.loads(calls[0]["input"]) == NOTIFY_FIELDS


def test_business_failure_is_a_typed_bridge_outcome(
    helper_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: result({"ok": False, "error": "title required"}, returncode=3),
    )
    outcome = client.notify(title="", kind="pr_open")
    assert outcome == NotifyOutcome(Lane.bridge, ok=False, error="title required")


@pytest.mark.parametrize(
    ("completed", "message"),
    [
        (result(None, returncode=1, stderr=b"connection refused\n"), "connection refused"),
        (result({"ok": True}, returncode=2), "does not match"),
    ],
)
def test_send_rejects_failed_bridge_process(
    helper_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[bytes],
    message: str,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed)
    with pytest.raises(OSError, match=message):
        client.send("ping")


@pytest.mark.parametrize("reply", [None, [], {"ok": "yes"}, {"ok": True, "version": 1}])
def test_send_rejects_malformed_reply(
    helper_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: result(reply))
    with pytest.raises((ValueError, OSError)):
        client.send("ping")


def test_notify_omits_none_optional_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake_send(operation: str, payload: object) -> dict[str, object]:
        seen.extend([operation, payload])
        return {"ok": True}

    monkeypatch.setattr(client, "send", fake_send)
    assert client.notify(title="t", kind="pr_open").ok
    assert seen == ["notify", {"kind": "pr_open", "title": "t"}]


def test_notify_launch_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = iter([None, NotifyOutcome(Lane.bridge, ok=True, error=None)])
    monkeypatch.setattr(client, "_try_bridge", lambda _payload: next(outcomes))
    monkeypatch.setattr(client, "_launch", lambda: True)
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    assert client.notify(**NOTIFY_FIELDS) == NotifyOutcome(Lane.bridge, ok=True, error=None)


def test_notify_not_installed_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_try_bridge", lambda _payload: None)
    monkeypatch.setattr(client, "_launch", lambda: False)
    assert client.notify(**NOTIFY_FIELDS) == NotifyOutcome(Lane.dropped, ok=False, error="helper not installed")


def test_notify_timeout_after_launch_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(client, "_try_bridge", lambda _payload: None)
    monkeypatch.setattr(client, "_launch", lambda: True)
    monkeypatch.setattr(client.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    monkeypatch.setattr(client, "LAUNCH_POLL_BUDGET", 0.5)
    assert client.notify(**NOTIFY_FIELDS) == NotifyOutcome(
        Lane.dropped, ok=False, error="helper unreachable after launch"
    )


def test_notify_never_raises_on_malformed_bridge_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "send", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(client, "_launch", lambda: False)
    assert client.notify(**NOTIFY_FIELDS).lane is Lane.dropped


def test_notify_drops_oversized_payload_without_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client,
        "_launch",
        lambda: (_ for _ in ()).throw(AssertionError("launch must not run")),
    )
    outcome = client.notify(title="t", kind="pr_open", body="x" * (client.PAYLOAD_CAP + 1))
    assert outcome == NotifyOutcome(Lane.dropped, ok=False, error="payload exceeds frame cap")
