import io
import json
import struct
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from captain_hook import cli
from captain_hook.snapshots.client import CURRENT_CLIENT, SnapshotClient
from captain_hook.snapshots.worker import empty_usage
from captain_hook.types import Event
from captain_hook.worker.protocol import ProtocolError, decode_snapshot_reply, read_message
from captain_hook.worker.runtime import ProductRuntime
from captain_hook.worker.service import BACKGROUND_SNAPSHOT_CLIENT
from tests.test_worker_runtime import Snapshot, request


@pytest.mark.parametrize(
    "lexeme,accepted", [("1e0", True), ("1.0", True), ("1.00000000000000001", False), ("9007199254740990.5", False)]
)
def test_legacy_frame_decoder_preserves_exact_snapshot_numbers(lexeme, accepted):
    response = {
        "protocol": 1,
        "op": "snapshot_result",
        "id": 1,
        "snapshot": {
            "schema": "captain.transcript/1",
            "response": {
                "schema": "cc-transcript.snapshot/1",
                "id": "fixture",
                "status": "ok",
                "complete": True,
                "data": {"kind": "released", "released": True},
                "cursor": None,
                "reason": None,
                "usage": empty_usage() | {"source_opens": 12345},
            },
        },
    }
    raw = json.dumps(response).replace('"source_opens": 12345', f'"source_opens": {lexeme}').encode()
    decoded = read_message(io.BytesIO(struct.pack(">I", len(raw)) + raw))
    if accepted:
        checked = decode_snapshot_reply(decoded)
        assert checked.snapshot["response"]["usage"]["source_opens"] == 1
        assert type(checked.snapshot["response"]["usage"]["source_opens"]) is int
    else:
        with pytest.raises(ProtocolError, match="invalid snapshot response"):
            decode_snapshot_reply(decoded)


def test_after_reply_builds_fresh_transcript_in_background_client_scope(tmp_path, monkeypatch):
    foreground, background = object(), object()
    clients = []
    events = []

    def loader(path):
        clients.append(CURRENT_CLIENT.get())
        return SimpleNamespace(path=path)

    def sync(event, evt, **kwargs):
        events.append(evt)
        assert evt.ctx.transcript.path == "/fixture.jsonl"
        evt.ctx.signal_evidence[(1, "any")] = ("stale",)
        evt.ctx.transcript.release()

    def after(event, evt, raw, session_dir):
        events.append(evt)
        assert evt.ctx.transcript.path == "/fixture.jsonl"
        assert evt.ctx.signal_evidence == {}
        evt.ctx.transcript.release()

    monkeypatch.setattr(cli, "dispatch", sync)
    monkeypatch.setattr(cli, "after_reply", after)
    token = CURRENT_CLIENT.set(foreground)
    try:
        _, run_background = cli.dispatch_event(
            tmp_path,
            Event.PreToolUse,
            {"transcript_path": "/fixture.jsonl"},
            session_dir=None,
            transcript_loader=loader,
        )
        CURRENT_CLIENT.set(background)
        run_background()
    finally:
        CURRENT_CLIENT.reset(token)
    assert clients == [foreground, background]
    assert events[0] is not events[1]
    assert events[0].ctx is not events[1].ctx


def test_runtime_freezes_both_clients_from_request_registry(monkeypatch):
    from captain_hook import app

    specs = {"fixture": ("Edit", {"file": "path", "hunks": "patch"})}
    snapshot = Snapshot(app.State(), tools=specs)
    foreground = SnapshotClient(lambda value: pytest.fail("no transport needed"))
    background = SnapshotClient(lambda value: pytest.fail("no transport needed"))
    observed = []

    def dispatch(*args, **kwargs):
        specs["fixture"][1]["file"] = "changed"
        monkeypatch.setattr(cli, "_registered_tools", {"other": ("Read", None)})
        observed.append(CURRENT_CLIENT.get().tool_registry())
        return None, lambda: observed.append(CURRENT_CLIENT.get().tool_registry())

    token = CURRENT_CLIENT.set(foreground)
    background_token = BACKGROUND_SNAPSHOT_CLIENT.set(background)
    runtime = ProductRuntime(
        registry_factory=lambda _: SimpleNamespace(get=lambda: snapshot),
        dispatcher=dispatch,
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    try:
        response, after = runtime.dispatch(request())
        assert response.status == "ok"
        after()
    finally:
        CURRENT_CLIENT.reset(token)
        BACKGROUND_SNAPSHOT_CLIENT.reset(background_token)
    assert observed[0] == observed[1]
    assert observed[0][0]["span_edit"]["file"] == "path"
    assert observed[0][0]["name"] == "fixture"


def test_cold_cli_binds_snapshot_scope_after_discovery_for_both_phases(tmp_path, monkeypatch):
    seen = []
    client = SnapshotClient(lambda value: pytest.fail("no transport needed"))

    @contextmanager
    def scope():
        assert seen == ["discover"]
        token = CURRENT_CLIENT.set(client)
        try:
            yield client
        finally:
            CURRENT_CLIENT.reset(token)

    def dispatch(*args, **kwargs):
        assert CURRENT_CLIENT.get() is client
        assert client.tool_registry() == []
        seen.append("foreground")
        return None, lambda: seen.append("background") if CURRENT_CLIENT.get() is client else pytest.fail("lost client")

    state = SimpleNamespace(root=tmp_path, discover=lambda: seen.append("discover") or [])
    monkeypatch.setattr("captain_hook.snapshots.client.client_scope", scope)
    monkeypatch.setattr(cli, "dispatch_event", dispatch)
    monkeypatch.setattr(cli, "setup_logging", lambda *args: None)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("{}"))
    cli.run_event(state, "PreToolUse")
    assert seen == ["discover", "foreground", "background"]
