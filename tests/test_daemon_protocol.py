from __future__ import annotations

import json
import os

import pytest

from capt_hook_client import client as thin_client
from capt_hook_client import key
from captain_hook.daemon import protocol

CLIENT = {"version": "", "build": "b-42", "pid": 111, "ppid": 222}


class FakeSocket:
    """A ``recv``-only stub streaming preset byte chunks, then EOF (empty bytes)."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


# --- request decode round-trips against the shipped client -----------------------------


def test_decode_event_request_from_client_build_request() -> None:
    raw = thin_client.build_request("PreToolUse", "/proj", None, "PAYLOAD-BYTES", async_=False)
    req = protocol.decode_request(json.dumps(raw).encode())

    assert req == protocol.Request(
        v=key.PROTOCOL,
        kind="event",
        client=protocol.ClientInfo(version="", build=raw["client"]["build"], pid=os.getpid(), ppid=os.getppid()),
        event="PreToolUse",
        async_=False,
        root="/proj",
        cwd=os.getcwd(),
        hooks=None,
        env=raw["env"],
        payload_raw="PAYLOAD-BYTES",
    )


def test_decode_event_request_async_and_hooks() -> None:
    raw = thin_client.build_request("Stop", "/r", ".claude/hooks", "{}", async_=True)
    req = protocol.decode_request(json.dumps(raw).encode())

    assert req.kind == "event"
    assert req.event == "Stop"
    assert req.async_ is True
    assert req.hooks == ".claude/hooks"
    assert req.payload_raw == "{}"


def test_decode_ping_request_from_client() -> None:
    req = protocol.decode_request(json.dumps(thin_client.ping_request()).encode())

    assert req.kind == "ping"
    assert req.event is None
    assert req.env == {}
    assert req.payload_raw == ""
    assert req.async_ is False


def test_decode_through_read_line_pipeline() -> None:
    raw = thin_client.build_request("PostToolUse", "/proj", None, "P", async_=False)
    sock = FakeSocket(json.dumps(raw).encode() + b"\nTRAILING-JUNK")

    req = protocol.decode_request(protocol.read_line(sock))

    assert req.event == "PostToolUse"
    assert req.payload_raw == "P"


@pytest.mark.parametrize("kind", ["ping", "status", "drain", "shutdown"])
def test_decode_control_kinds(kind: str) -> None:
    req = protocol.decode_request(json.dumps({"v": 1, "kind": kind, "client": CLIENT}).encode())

    assert req.kind == kind
    assert req.event is None
    assert req.env == {}
    assert req.async_ is False
    assert req.client == protocol.ClientInfo(version="", build="b-42", pid=111, ppid=222)


def test_control_kinds_membership() -> None:
    assert protocol.CONTROL_KINDS == {"ping", "status", "drain", "shutdown"}
    assert protocol.REQUEST_KINDS == {"event", "ping", "status", "drain", "shutdown"}


# --- malformed / wrong-shape rejection -------------------------------------------------


def test_decode_malformed_json_raises() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(b"{not valid json")


@pytest.mark.parametrize(
    "wire",
    [
        b"[]",
        b'"scalar"',
        b"12",
        b"null",
        json.dumps({"kind": "ping", "client": CLIENT}).encode(),  # missing v
        json.dumps({"v": 1, "client": CLIENT}).encode(),  # missing kind
        json.dumps({"v": 1, "kind": "event"}).encode(),  # missing client
        json.dumps({"v": 1, "kind": "bogus", "client": CLIENT}).encode(),  # unknown kind
        json.dumps({"v": "1", "kind": "ping", "client": CLIENT}).encode(),  # v not int
        json.dumps({"v": 1, "kind": "ping", "client": {"version": "", "build": "b", "pid": "x", "ppid": 2}}).encode(),
    ],
    ids=[
        "list",
        "string-scalar",
        "int-scalar",
        "null",
        "missing-v",
        "missing-kind",
        "missing-client",
        "unknown-kind",
        "v-not-int",
        "client-pid-not-int",
    ],
)
def test_decode_invalid_shape_raises(wire: bytes) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(wire)


# --- response encode is accepted verbatim by the shipped client ------------------------


def test_encode_response_roundtrips_through_client_validate() -> None:
    resp = protocol.Response(
        v=key.PROTOCOL,
        daemon=protocol.DaemonInfo(version="9.4.0", build="db", pid=999),
        status="ok",
        stdout="OUT",
        stderr="ERR",
        exit=2,
        elapsed_ms=12.5,
    )
    wire = protocol.encode_response(resp)

    assert wire.endswith(b"\n")
    validated = thin_client.validate(wire)
    assert validated == {
        "v": key.PROTOCOL,
        "daemon": {"version": "9.4.0", "build": "db", "pid": 999},
        "status": "ok",
        "stdout": "OUT",
        "stderr": "ERR",
        "exit": 2,
        "elapsed_ms": 12.5,
    }
    assert thin_client.emit_response(validated) == 2


def test_encode_response_preserves_wide_unicode() -> None:
    payload = "café-\U0001f680-" * 4000
    resp = protocol.Response(
        v=key.PROTOCOL, daemon=protocol.DaemonInfo(version="v", build="b", pid=1), status="ok", stdout=payload
    )

    assert thin_client.validate(protocol.encode_response(resp))["stdout"] == payload


# --- read_line framing -----------------------------------------------------------------


def test_read_line_splits_at_first_newline() -> None:
    assert protocol.read_line(FakeSocket(b'{"a":1}\nLEFTOVER')) == b'{"a":1}'


def test_read_line_reassembles_chunks() -> None:
    assert protocol.read_line(FakeSocket(b'{"a"', b":1", b"}\n")) == b'{"a":1}'


def test_read_line_eof_before_newline_raises() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.read_line(FakeSocket(b'{"a":1}'))


def test_read_line_oversize_no_newline_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_LINE", 8)
    with pytest.raises(protocol.LineTooLong):
        protocol.read_line(FakeSocket(b"aaaaa", b"bbbbb", b"ccccc"))


def test_read_line_newline_past_limit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_LINE", 4)
    with pytest.raises(protocol.LineTooLong):
        protocol.read_line(FakeSocket(b"aaaaaaaa\n"))


# --- sun_path validation ---------------------------------------------------------------


def test_validate_sun_path_accepts_short() -> None:
    path = "/tmp/x.sock"
    assert protocol.validate_sun_path(path) == path


def test_validate_sun_path_boundary() -> None:
    ok = "/" + "a" * (protocol.SUN_PATH_MAX - 2)
    assert len(ok.encode()) == protocol.SUN_PATH_MAX - 1
    assert protocol.validate_sun_path(ok) == ok

    too_long = "/" + "a" * (protocol.SUN_PATH_MAX - 1)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_sun_path(too_long)


# Roots are realpath-stable (no existing symlink components) so the hexes hold on macOS and
# Linux CI; the third case pins that session/config-dir vars stay out of the worker subset.
GOLDEN_KEYS = [
    ("/proj", {}, "79903f0c2002c352"),
    ("/srv/app", {"CAPTAIN_HOOK_STATE_DIR": "/s", "HOOKS_X": "1"}, "0bad0384da55243d"),
    (
        "/opt/repo",
        {
            "XDG_CACHE_HOME": "/cache",
            "HOOKS_A": "a",
            "HOOKS_B": "b",
            "CLAUDE_CODE_SESSION_ID": "ignored",
            "CLAUDE_CONFIG_DIR": "/pool",
        },
        "cac3ed2855211997",
    ),
]


@pytest.mark.parametrize("root,env,expected", GOLDEN_KEYS, ids=["bare", "state+hooks", "excludes-session-and-config"])
def test_worker_key_golden(root: str, env: dict[str, str], expected: str) -> None:
    assert os.path.realpath(root) == root, "golden root must be realpath-stable across platforms"
    assert key.worker_key(root, env) == expected


# --- client-parity: protocol re-exports the SAME key objects, never a copy -------------


def test_protocol_reexports_are_key_objects() -> None:
    assert protocol.PROTOCOL is key.PROTOCOL
    assert protocol.PROTOCOL == 1
    assert protocol.worker_key is key.worker_key
    assert protocol.socket_path is key.socket_path
    assert protocol.lock_path is key.lock_path
    assert protocol.meta_path is key.meta_path
    assert protocol.log_path is key.log_path
    assert protocol.run_dir is key.run_dir
    assert protocol.request_env is key.request_env
    assert protocol.build_fingerprint is key.build_fingerprint
