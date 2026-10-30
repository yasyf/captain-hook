from __future__ import annotations

import pytest

from captain_hook.snapshots.client import HOST_SCHEMA, EvidenceIncomplete, SnapshotClient
from captain_hook.snapshots.worker import failure
from captain_hook.tickets import Context
from captain_hook.tickets import decide as deciding


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("projection exceeds serialized output limit", "serialized_projection"),
        ("activity window of 1 turns (0..1) retains 100 bytes", "activity_window"),
        ("encoded reply exceeds page bound", "encoded_page"),
        ("response data exceeds output budget", "response_data"),
        ("reservation output budget exhausted", "output_reservation"),
        ("graph record exceeds remaining output budget", "graph_record"),
        ("projection exceeds request output budget", "request_projection"),
        ("snapshot record needs 2000000 bytes, over the 1048576-byte record bound", "snapshot_record"),
        ("snapshot page needs 20000000 bytes, over the 16777216-byte page bound", "snapshot_page"),
        ("snapshot page of 257 records exceeds the 256-record page bound", "snapshot_page"),
        ("private native failure", None),
    ],
)
def test_output_refusals_log_only_classified_metadata(reason, expected, logcap):
    private = "/Users/private/session.jsonl sk-fixture-secret"
    full_reason = f"{reason}: {private}"

    def exchange(wrapper):
        request = wrapper["request"]
        return {"schema": HOST_SCHEMA, "response": failure(request["id"], "output_limit", full_reason)}

    client = SnapshotClient(exchange)
    with pytest.raises(EvidenceIncomplete) as caught:
        list(client.pages("query", query={"kind": "turns"}, view={"private": private}))
    assert (caught.value.status, caught.value.reason) == ("output_limit", full_reason)
    [record] = [record for record in logcap.records if record.message.startswith("snapshot request failed")]
    assert f"output_limit_kind={expected!r}" in record.message
    assert "projection_kind='turns'" in record.message
    assert private not in logcap.text
    assert full_reason not in logcap.text


@pytest.mark.parametrize("kind", ["turns", "events"])
def test_query_kind_logs_the_plain_whitelist_literal(kind, logcap):
    class PrivateKind(str):
        def __repr__(self) -> str:
            return "private-repr-sk-fixture-secret"

    def exchange(wrapper):
        request = wrapper["request"]
        return {"schema": HOST_SCHEMA, "response": failure(request["id"], "output_limit", "private native reason")}

    client = SnapshotClient(exchange)
    with pytest.raises(EvidenceIncomplete) as caught:
        list(client.pages("query", query={"kind": PrivateKind(kind)}))
    assert caught.value.reason == "private native reason"
    assert f"projection_kind={kind!r}" in logcap.text
    assert "private-repr-sk-fixture-secret" not in logcap.text
    assert "private native reason" not in logcap.text


@pytest.mark.parametrize("query_kind", ["private-query-kind-sk-fixture-secret", ["turns"]])
def test_unknown_query_kind_is_not_logged(query_kind, logcap):
    private = "private-query-kind-sk-fixture-secret"

    def exchange(wrapper):
        request = wrapper["request"]
        return {"schema": HOST_SCHEMA, "response": failure(request["id"], "output_limit", private)}

    client = SnapshotClient(exchange)
    with pytest.raises(EvidenceIncomplete):
        list(client.pages("query", query={"kind": query_kind}))
    assert "projection_kind=None" in logcap.text
    assert private not in logcap.text


@pytest.mark.parametrize("fails", [False, True])
def test_ticket_stage_records_duration_and_preserves_errors(fails, logcap, monkeypatch):
    ticks = iter((10.0, 10.125))
    monkeypatch.setattr(deciding, "perf_counter", lambda: next(ticks))
    ctx = Context.fake("test.write", {"channel": "private-channel"}, payload={"text": "private-body"})
    error = RuntimeError("private-error")
    if fails:
        with pytest.raises(RuntimeError) as caught, deciding.decision_stage(ctx, "jev"):
            raise error
        assert caught.value is error
    else:
        with deciding.decision_stage(ctx, "jev"):
            pass
    [record] = [record for record in logcap.records if record.message.startswith("ticket decision stage")]
    assert "kind='test.write'" in record.message
    assert "stage='jev'" in record.message
    assert "elapsed_ms=125.0" in record.message
    for private in ("private-channel", "private-body", "private-error"):
        assert private not in logcap.text
