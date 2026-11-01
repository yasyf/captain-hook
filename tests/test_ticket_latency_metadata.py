from __future__ import annotations

import pytest

from captain_hook.conditions import UserSaid
from captain_hook.snapshots.client import HOST_SCHEMA, EvidenceIncomplete, SnapshotClient
from captain_hook.snapshots.worker import failure
from captain_hook.tickets import Allow, Context, Reach, Rule, Tickets
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


@pytest.mark.parametrize("kind", ["turns", "events", "interaction_keys", "interaction_winners", "interaction_records", "meta_messages"])
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
    [record] = [record for record in logcap.records if record.message.startswith("snapshot request failed")]
    assert f"projection_kind={kind!r}" in record.message
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
    [record] = [record for record in logcap.records if record.message.startswith("snapshot request failed")]
    assert "projection_kind=None" in record.message
    assert private not in logcap.text


@pytest.mark.parametrize("fails", [False, True])
def test_ticket_stage_records_duration_and_preserves_errors(fails, logcap, monkeypatch):
    ticks = iter((10.0, 10.125))
    monkeypatch.setattr(deciding, "perf_counter", lambda: next(ticks))
    ctx = Context.fake("test.write", {"channel": "private-channel"}, payload={"text": "private-body"})
    error = RuntimeError("private-error")
    if fails:
        with pytest.raises(RuntimeError) as caught, deciding.decision_stage(ctx.kind, "jev"):
            raise error
        assert caught.value is error
    else:
        with deciding.decision_stage(ctx.kind, "jev"):
            pass
    [record] = [record for record in logcap.records if record.message.startswith("ticket decision stage")]
    assert "kind='test.write'" in record.message
    assert "stage='jev'" in record.message
    assert "elapsed_ms=125.0" in record.message
    for private in ("private-channel", "private-body", "private-error"):
        assert private not in logcap.text


def test_rule_stage_timings_include_misses_and_preserve_covering_fast_path(logcap, monkeypatch):
    ticks = iter((10.0, 10.125, 20.0, 20.25))
    monkeypatch.setattr(deciding, "perf_counter", lambda: next(ticks))
    ctx = Context.fake("test.stage", {"channel": "private-channel"}, said=["post it"])
    tickets = Tickets(
        "test.stage",
        ("channel",),
        Reach.PLACE,
        rules=(Rule("miss", UserSaid("unrelated")), Rule("match", UserSaid("post it"))),
        deny="Ask the user.",
    )
    verdict = tickets.decide(ctx)
    assert isinstance(verdict, Allow)
    assert verdict.ticket.rule == "match"
    records = [record for record in logcap.records if record.message.startswith("ticket decision stage")]
    assert len(records) == 2
    assert "stage='rule:miss'" in records[0].message
    assert "elapsed_ms=125.0" in records[0].message
    assert "stage='rule:match'" in records[1].message
    assert "elapsed_ms=250.0" in records[1].message
    covering = tickets.decide(ctx)
    assert isinstance(covering, Allow) and not covering.minted
    assert covering.ticket.id == verdict.ticket.id
    assert len([record for record in logcap.records if record.message.startswith("ticket decision stage")]) == 2
    assert "private-channel" not in logcap.text
    assert "post it" not in logcap.text


@pytest.mark.parametrize(
    ("reason", "needed", "budget", "scope"),
    [
        ("snapshot record needs 2000000 bytes, over the 1048576-byte record bound", 2000000, 1048576, "record"),
        ("snapshot record needs 400 bytes, over the 8-byte remaining output budget", 400, 8, "remaining_output"),
        ("snapshot page needs 20000000 bytes, over the 16777216-byte page bound", 20000000, 16777216, "page"),
    ],
)
def test_output_refusals_report_native_size_operands_without_the_reason(reason, needed, budget, scope, logcap):
    private = "private-sk-fixture-secret"

    def exchange(wrapper):
        return {"schema": HOST_SCHEMA, "response": failure(wrapper["request"]["id"], "output_limit", f"{reason}: {private}")}

    client = SnapshotClient(exchange)
    with pytest.raises(EvidenceIncomplete) as caught:
        list(client.pages("query", query={"kind": "interaction_records"}))
    assert caught.value.reason == f"{reason}: {private}"
    [record] = [record for record in logcap.records if record.message.startswith("snapshot request failed")]
    assert f"output_needed_bytes={needed}" in record.message
    assert f"output_budget_bytes={budget}" in record.message
    assert f"output_budget_scope={scope!r}" in record.message
    assert "projection_kind='interaction_records'" in record.message
    assert private not in logcap.text


@pytest.mark.parametrize("reason", [None, [], "private-1234", "snapshot record needs " + "9" * 100 + " bytes"])
def test_unrecognized_native_sizes_do_not_change_the_original_refusal(reason):
    from captain_hook.snapshots.client import output_limit_sizes

    assert output_limit_sizes(reason) == {}


def test_decision_stages_and_mint_share_the_snapshot_request_prefix(logcap, monkeypatch):
    import os

    from captain_hook.snapshots.client import CURRENT_CLIENT

    monkeypatch.setattr(deciding, "decision_build", lambda: "12.107.99")
    requests = []

    def exchange(wrapper):
        requests.append(wrapper["request"])
        return {"schema": HOST_SCHEMA, "response": failure(wrapper["request"]["id"], "missing", "private-reason")}

    client = SnapshotClient(exchange)
    token = CURRENT_CLIENT.set(client)
    ctx = Context.fake("trace.write", {"channel": "private-channel"}, said=["private-yes"])
    tickets = Tickets("trace.write", ("channel",), Reach.PLACE, rules=(Rule("match", UserSaid("private-yes")),), deny="Ask.")
    try:
        with pytest.raises(EvidenceIncomplete), deciding.decision_stage(ctx.kind, "jev"):
            list(client.pages("query", query={"kind": "turns"}))
        verdict = tickets.decide(ctx)
        assert isinstance(verdict, Allow)
    finally:
        CURRENT_CLIENT.reset(token)
        client.close()
    records = [record for record in logcap.records if record.message.startswith(("ticket decision stage", "ticket minted"))]
    assert len(records) == 3
    assert requests[0]["id"].startswith(client.request_prefix + "-")
    for record in records:
        assert f"request_prefix={client.request_prefix!r}" in record.message
        assert "capt_build='12.107.99'" in record.message
        assert f"worker_pid={os.getpid()}" in record.message
    for private in ("private-channel", "private-yes", "private-reason"):
        assert private not in logcap.text


def test_stage_duration_excludes_cold_log_metadata_lookup(logcap, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(deciding, "perf_counter", lambda: clock[0])

    def cold_build():
        clock[0] += 2.0
        return "12.107.99"

    monkeypatch.setattr(deciding, "decision_build", cold_build)
    ctx = Context.fake("test.lookup", {})
    with deciding.decision_stage(ctx.kind, "jev"):
        clock[0] += 0.01
    [record] = [record for record in logcap.records if record.message.startswith("ticket decision stage")]
    assert "elapsed_ms=10.0" in record.message
    assert clock[0] == pytest.approx(12.01)


@pytest.mark.parametrize("kind", ["slack.write", "test.write"], ids=["slack", "other"])
@pytest.mark.parametrize("covering", [False, True], ids=["mint", "covering"])
def test_slack_outer_ticket_timings(kind, covering, logcap, monkeypatch):
    import os
    from types import SimpleNamespace

    from captain_hook.snapshots.client import CURRENT_CLIENT
    from captain_hook.tickets import declare as declaring

    clock = [10.0]
    calls = []
    ctx = Context.fake(kind, {}, said=["private-evidence"])
    ticket = SimpleNamespace(id="private-ticket", rule="match")
    tickets = Tickets(kind, (), Reach.PLACE, rules=(Rule("match", UserSaid()),), deny="Ask.")

    def context(self, evt, action=None):
        calls.append("context")
        clock[0] += 0.002
        return ctx

    def lookup(*args):
        calls.append("covering")
        clock[0] += 0.003
        return ticket if covering else None

    def found(*args):
        calls.append("rule")
        clock[0] += 0.004
        return [object()]

    def mint(self, *args):
        calls.append("mint")
        clock[0] += 0.005
        return ticket

    monkeypatch.setattr(deciding, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(deciding, "decision_build", lambda: "12.108.99")
    monkeypatch.setattr(Tickets, "context", context)
    monkeypatch.setattr(Tickets, "mint", mint)
    monkeypatch.setattr(declaring.store, "covering", lookup)
    monkeypatch.setattr(declaring, "found", found)
    client = SnapshotClient(lambda _: pytest.fail("no native request expected"))
    token = CURRENT_CLIENT.set(client)
    try:
        verdict = tickets.check(object())
    finally:
        CURRENT_CLIENT.reset(token)
        client.close()
    assert isinstance(verdict, Allow) and verdict.ticket is ticket and verdict.minted is not covering
    assert calls == (["context", "covering"] if covering else ["context", "covering", "rule", "mint"])
    stages = [record for record in logcap.records if record.message.startswith("ticket decision stage")]
    expected = {} if covering else {"rule:match": 4.0}
    if kind == "slack.write":
        expected |= {"check": 5.0 if covering else 14.0, "context": 2.0, "covering": 3.0}
        if not covering:
            expected["mint"] = 5.0
    if kind == "slack.write":
        assert any("stage='check'" in record.message for record in stages), "SLACK_OUTER_STAGE_MISSING: check"
    assert len(stages) == len(expected)
    for stage, duration in expected.items():
        matching = [record for record in stages if f"stage={stage!r}" in record.message]
        assert len(matching) == 1, f"SLACK_OUTER_STAGE_MISSING: {stage}"
        assert f"elapsed_ms={duration!r}" in matching[0].message
    for record in stages:
        assert f"kind={kind!r}" in record.message
        assert f"request_prefix={client.request_prefix!r}" in record.message
        assert "capt_build='12.108.99'" in record.message
        assert f"worker_pid={os.getpid()}" in record.message
        assert "private-evidence" not in record.message
        assert "private-ticket" not in record.message


@pytest.mark.parametrize("kind", ["slack.write", "test.write"], ids=["slack", "other"])
@pytest.mark.parametrize("boundary", ["context", "covering", "mint"])
@pytest.mark.parametrize("incomplete", [False, True], ids=["ordinary", "incomplete"])
def test_slack_outer_ticket_exception_timings(kind, boundary, incomplete, logcap, monkeypatch):
    from types import SimpleNamespace

    from captain_hook.tickets import Deny
    from captain_hook.tickets import declare as declaring

    clock = [10.0]
    calls = []
    ctx = Context.fake(kind, {})
    ticket = SimpleNamespace(id="private-ticket")
    error = EvidenceIncomplete("incomplete", "private-error") if incomplete else RuntimeError("private-error")
    tickets = Tickets(kind, (), Reach.PLACE, rules=(Rule("match", UserSaid()),), deny="Ask.")

    def step(name, value):
        calls.append(name)
        clock[0] += 0.001
        if name == boundary:
            raise error
        return value

    monkeypatch.setattr(deciding, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(Tickets, "context", lambda *args: step("context", ctx))
    monkeypatch.setattr(declaring.store, "covering", lambda *args: step("covering", None))
    monkeypatch.setattr(declaring, "found", lambda *args: step("rule", [object()]))
    monkeypatch.setattr(Tickets, "mint", lambda *args: step("mint", ticket))
    verdict = tickets.check(object())
    assert isinstance(verdict, Deny) and verdict.message == "Ask."
    assert "private-error" in verdict.detail
    expected_calls = ["context"] if boundary == "context" else ["context", "covering"] if boundary == "covering" else ["context", "covering", "rule", "mint"]
    assert calls == expected_calls
    stages = [record for record in logcap.records if record.message.startswith("ticket decision stage")]
    expected = ["rule:match"] if boundary == "mint" else []
    if kind == "slack.write":
        expected += [name for name in expected_calls if name != "rule"] + ["check"]
    assert len(stages) == len(expected)
    for stage in expected:
        assert sum(f"stage={stage!r}" in record.message for record in stages) == 1
    assert all("private-error" not in record.message and "private-ticket" not in record.message for record in stages)
    if boundary != "context":
        calls.clear()
        with pytest.raises(type(error)) as caught:
            tickets.decide(ctx)
        assert caught.value is error
        assert calls == expected_calls[1:]
