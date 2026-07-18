"""The ``~/.capt-hook/helper.sock`` client: wire-golden encode, reply decode, and the notify loop.

This module drives the real :mod:`captain_hook.helper.client` against an in-process AF_UNIX
server (the conftest notify stub opts this module out), asserting the bytes it sends and the
outcomes it returns match the frozen ``tests/fixtures/helper-sock-v1.golden.jsonl``.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from captain_hook.helper import client
from captain_hook.helper.client import Lane, NotifyOutcome

if TYPE_CHECKING:
    from collections.abc import Iterator

GOLDEN = Path(__file__).parent / "fixtures" / "helper-sock-v1.golden.jsonl"
ROWS = GOLDEN.read_bytes().splitlines(keepends=True)
PING_REQUEST, PING_REPLY, NOTIFY_REQUEST, NOTIFY_OK, NOTIFY_FAIL = ROWS

NOTIFY_FIELDS = {
    "title": "Block force-pushes",
    "kind": "pr_open",
    "subtitle": "captain-hook",
    "body": "Rule guard-rm-rf opened",
    "url": "https://github.com/yasyf/captain-hook/pull/12",
    "repo": "github.com/yasyf/captain-hook",
}


class FakeHelper:
    """A one-reply-per-connection AF_UNIX server that records every request line it received."""

    def __init__(self, path: Path, reply: bytes | None) -> None:
        self.path = path
        self.reply = reply
        self.received: list[bytes] = []
        self._running = True
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                self.received.append(_read_line(conn))
                if self.reply is not None:
                    conn.sendall(self.reply)

    def close(self) -> None:
        self._running = False
        self._sock.close()


def _read_line(conn: socket.socket) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        if not (chunk := conn.recv(65536)):
            break
        buf.extend(chunk)
    return bytes(buf)


@pytest.fixture
def helper_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short /tmp path so the AF_UNIX socket stays under macOS's sun_path limit.
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    monkeypatch.setenv("CAPT_HOOK_HELPER_DIR", str(directory))
    yield directory


def serve(helper_dir: Path, reply: bytes | None) -> FakeHelper:
    return FakeHelper(client.socket_path(), reply)


def test_paths_honor_override(helper_dir: Path) -> None:
    assert client.helper_dir() == helper_dir
    assert client.socket_path() == helper_dir / "helper.sock"
    assert client.status_path() == helper_dir / "status.json"


def test_encode_ping_matches_golden() -> None:
    assert client.encode({"v": client.PROTOCOL, "op": "ping"}) == PING_REQUEST


def test_ping_send_transmits_row1_decodes_row2(helper_dir: Path) -> None:
    server = serve(helper_dir, PING_REPLY)
    try:
        reply = client.send({"v": client.PROTOCOL, "op": "ping"})
    finally:
        server.close()
    assert server.received == [PING_REQUEST]
    assert reply == json.loads(PING_REPLY)
    assert reply == {"v": 1, "ok": True, "version": "1.0.0"}


def test_notify_sends_golden_row3_and_decodes_success(helper_dir: Path) -> None:
    server = serve(helper_dir, NOTIFY_OK)
    try:
        outcome = client.notify(**NOTIFY_FIELDS)
    finally:
        server.close()
    assert server.received == [NOTIFY_REQUEST]
    assert outcome == NotifyOutcome(Lane.socket, ok=True, error=None)


def test_notify_decodes_failure_reply(helper_dir: Path) -> None:
    server = serve(helper_dir, NOTIFY_FAIL)
    try:
        outcome = client.notify(**NOTIFY_FIELDS)
    finally:
        server.close()
    assert outcome == NotifyOutcome(Lane.socket, ok=False, error="unknown op")


def test_notify_omits_none_optional_fields(helper_dir: Path) -> None:
    server = serve(helper_dir, NOTIFY_OK)
    try:
        client.notify(title="t", kind="pr_open")
    finally:
        server.close()
    assert server.received == [b'{"v":1,"op":"notify","kind":"pr_open","title":"t"}\n']


def test_notify_launch_retry_succeeds(helper_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "LAUNCH_POLL_INTERVAL", 0.02)
    servers: list[FakeHelper] = []

    def fake_launch() -> bool:
        servers.append(serve(helper_dir, NOTIFY_OK))
        return True

    monkeypatch.setattr(client, "_launch", fake_launch)
    try:
        outcome = client.notify(**NOTIFY_FIELDS)
    finally:
        for server in servers:
            server.close()
    assert outcome == NotifyOutcome(Lane.socket, ok=True, error=None)
    assert servers and servers[0].received == [NOTIFY_REQUEST]


def test_notify_not_installed_fast_path(helper_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_launch", lambda: False)
    outcome = client.notify(**NOTIFY_FIELDS)
    assert outcome == NotifyOutcome(Lane.dropped, ok=False, error="helper not installed")


def test_notify_timeout_after_launch_drops(helper_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "SOCKET_TIMEOUT", 0.15)
    monkeypatch.setattr(client, "LAUNCH_POLL_BUDGET", 0.3)
    monkeypatch.setattr(client, "LAUNCH_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(client, "_launch", lambda: True)
    server = serve(helper_dir, reply=None)  # accepts, never replies
    try:
        outcome = client.notify(**NOTIFY_FIELDS)
    finally:
        server.close()
    assert outcome == NotifyOutcome(Lane.dropped, ok=False, error="helper unreachable after launch")


def test_notify_never_raises_on_garbage_reply(helper_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_launch", lambda: False)
    server = serve(helper_dir, b"not json\n")
    try:
        outcome = client.notify(**NOTIFY_FIELDS)
    finally:
        server.close()
    assert outcome.lane is Lane.dropped
    assert outcome.ok is False


def test_send_raises_json_error_on_garbage(helper_dir: Path) -> None:
    server = serve(helper_dir, b"not json\n")
    try:
        with pytest.raises(json.JSONDecodeError):
            client.send({"v": client.PROTOCOL, "op": "ping"})
    finally:
        server.close()


def test_notify_returns_typed_outcome_on_non_dict_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "send", lambda *_args, **_kw: json.loads("null"))
    outcome = client.notify(title="t", kind="pr_open")
    assert outcome == NotifyOutcome(Lane.socket, ok=False, error="malformed reply: None")


def test_notify_drops_oversized_request_without_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kw: object) -> bool:
        raise AssertionError("launch must not run for an oversized request")

    monkeypatch.setattr(client, "_launch", boom)
    outcome = client.notify(title="t", kind="pr_open", body="x" * (client.LINE_CAP + 1))
    assert outcome == NotifyOutcome(Lane.dropped, ok=False, error="request exceeds line cap")
