from __future__ import annotations

import json

import pytest

from captain_hook import T
from captain_hook.context import HookContext
from captain_hook.events import PreToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete, FixedLease
from captain_hook.testing.helpers import fixture_line, fixture_session
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.tickets.context import Context, TranscriptSources
from captain_hook.tickets.records import Proposal
from captain_hook.transcripts import ROOT_EXCERPT_BYTES, lazy_transcript, load_transcript, root_transcript


def lines(messages):
    return "".join(json.dumps(fixture_line(index, message), ensure_ascii=False) + "\n" for index, message in enumerate(messages))


def question(ident, chosen="Yes", seconds=0):
    questions = [{"question": "Post to C1?", "options": [{"label": "Yes"}, {"label": "No"}]}]
    ask = T.tool("AskUserQuestion", id=ident, questions=questions)
    return [
        T.assistant(ask),
        T.user(T.result(f'User has answered your questions: "Post to C1?"="{chosen}".', of=ask),
            toolUseResult={"questions": questions, "answers": {"Post to C1?": chosen}, "annotations": {}},
            timestamp=f"2026-01-01T00:00:{seconds:02d}Z"),
    ]


def event(tmp_path, acting, root=None, root_path=None):
    return PreToolUseEvent(
        _raw={"session_id": "fixture", "tool_name": "slack_reply", "tool_use_id": "current", "tool_input": {}},
        ctx=HookContext(session=SessionStore(tmp_path), transcript=acting, settings=None,
            root_transcript=root, root_path=root_path),
    )


@pytest.mark.parametrize("explicit_standard_loader", [False, True])
@pytest.mark.parametrize("unrelated", [32, 4096])
def test_native_answers_match_legacy_without_projecting_unrelated_calls(tmp_path, monkeypatch, unrelated, explicit_standard_loader):
    from captain_hook.tickets import context as source_module

    monkeypatch.setattr(source_module, "board_state", lambda tree, cwd: {})
    calls = [T.user("start")]
    for index in range(unrelated):
        calls.append(T.assistant(T.tool("Bash", id=f"bash-{index}", command=f"echo {index}")))
    calls += question("duplicate", "Yes", 1) + question("other", "No", 2) + question("duplicate", "No", 2)
    overwritten = T.tool("Bash", id="other", command="echo replaced")
    calls.append(T.assistant(overwritten))
    path = tmp_path / "acting.jsonl"
    path.write_text(lines(calls))
    expected = TranscriptSources(event(tmp_path, fixture_session(calls)), ("C1",)).answers()
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    requests = []
    exchange = fixture.client._exchange

    def recorded(wrapper):
        requests.append(wrapper["request"])
        return exchange(wrapper)

    monkeypatch.setattr(fixture.client, "_exchange", recorded)
    acting = lazy_transcript(path, loader=load_transcript if explicit_standard_loader else None)
    try:
        actual = TranscriptSources(event(tmp_path, acting), ("C1",)).answers()
        assert actual == expected
        queries = [request["query"] for request in requests if request["operation"] == "query"]
        assert all(query["kind"] != "turns" for query in queries)
        assert [query["keys"] for query in queries if query["kind"] == "interaction_records"] == [["duplicate"]]
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
        print(f"ticket_native_answers unrelated={unrelated} selected_calls=1 answers={len(actual)}")
    finally:
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_root_window_is_shared_but_later_properties_reauthorize_current_source(tmp_path, monkeypatch):
    from captain_hook.tickets import context as source_module

    monkeypatch.setattr(source_module, "board_state", lambda tree, cwd: {})
    root_path = tmp_path / "root.jsonl"
    padding = json.dumps({"type": "queue-operation", "ignored": "x" * 1000}) + "\n"
    root_body = padding * (ROOT_EXCERPT_BYTES // len(padding) + 20) + lines([T.user("C1 release"), *question("root-question")])
    root_path.write_text(root_body)
    acting_path = tmp_path / "acting.jsonl"
    acting_path.write_text(lines([T.user("agent brief"), T.assistant(T.tool("Bash", id="touch", command="echo C1"))]))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    acting, root = lazy_transcript(acting_path), root_transcript(root_path, 256)
    try:
        for path in (root_path, acting_path):
            warmed = fixture.load(path)
            warmed.release()
        before = fixture.client.call("stats")["data"]["counters"]["source_bytes_read"]
        evt = event(tmp_path, acting, root, root_path)
        ctx = Context.of(evt, "test.write", Proposal({"channel": "C1"}, {"text": "reply"}))
        assert fixture.client.call("stats")["data"]["counters"]["source_bytes_read"] == before
        assert ctx.answers
        cold = fixture.client.call("stats")["data"]["counters"]["source_bytes_read"] - before
        assert cold == ROOT_EXCERPT_BYTES + 1
        assert ctx.said
        assert ctx.touches
        assert fixture.client.call("stats")["data"]["counters"]["source_bytes_read"] - before == cold
        newer = Context.of(evt, "test.write", Proposal({"channel": "C1"}, {"text": "reply"}))
        with root_path.open("a") as source:
            source.write(lines([T.user("No, do not post to C1", timestamp="2026-01-01T00:00:03Z")]))
        assert any("No, do not post" in evidence.quote for evidence in newer.said)
        assert fixture.client.call("stats")["data"]["counters"]["source_bytes_read"] - before > cold
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
        print(f"ticket_root_window cold_bytes={cold} subsequent_property_bytes=0")
    finally:
        root.release()
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_closed_source_branch_does_not_bypass_its_lifetime(tmp_path):
    acting = lazy_transcript(tmp_path / "not-created.jsonl")
    acting.release()
    sources = TranscriptSources(event(tmp_path, acting), ())
    with pytest.raises(EvidenceIncomplete, match="branch is closed"):
        sources.answers()


def test_fixed_lease_does_not_reacquire_on_stale_reply():
    from tests.test_snapshot_client import description, response
    from captain_hook.snapshots.client import SnapshotClient

    requests = []
    def exchange(wrapper):
        request = wrapper["request"]
        requests.append(request)
        result = response(request, None)
        result["response"].update(status="stale_handle", complete=False, reason="gone")
        return result
    client = SnapshotClient(exchange)
    source = description()
    source["lease_expires_unix_ms"] = 0
    lease = FixedLease(client, source)
    try:
        with pytest.raises(EvidenceIncomplete, match="fixed evidence generation expired"):
            lease.require()
        assert [request["operation"] for request in requests] == ["renew"]
    finally:
        client._leases.discard(lease)


def test_unread_property_rechecks_authority_after_answers(tmp_path, monkeypatch):
    from captain_hook.tickets import context as source_module

    monkeypatch.setattr(source_module, "board_state", lambda tree, cwd: {})
    path = tmp_path / "acting.jsonl"
    path.write_text(lines([T.user("C1 permission"), *question("q")]))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    acting = lazy_transcript(path)
    try:
        ctx = Context.of(event(tmp_path, acting), "test.write", Proposal({"channel": "C1"}, {"text": "reply"}))
        assert ctx.answers
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        fixture.context["authority"] = {"kind": "restricted_roots", "roots": [str(unrelated)], "effective_uid": fixture.context["authority"]["effective_uid"]}
        with pytest.raises(EvidenceIncomplete) as failure:
            _ = ctx.said
        assert failure.value.status == "permission_denied"
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
    finally:
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_board_wait_begins_after_all_transcript_queries_and_lease_release(tmp_path, monkeypatch):
    from captain_hook.tickets import context as source_module

    path = tmp_path / "acting.jsonl"
    payload = {"type": "slack.action", "action_ts": "1.2", "label": "No", "waits": True,
        "question": "Post to C1?", "options": ["Yes", "No"]}
    text = f'<channel source="{source_module.CARD_SERVER}" type="slack.action">\n{json.dumps(payload)}\n</channel>'
    path.write_text(lines([T.user("start"), *question("q"), T.user(text, isMeta=True)]))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    acting = lazy_transcript(path)
    boards = []

    def board(tree, cwd):
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
        boards.append(tree)
        path.unlink()
        return {}

    monkeypatch.setattr(source_module, "board_state", board)
    try:
        answers = TranscriptSources(event(tmp_path, acting), ("C1",)).answers()
        assert [answer.id for answer in answers] == ["ask:q#0", "card:1.2"]
        assert boards == ["fixture"]
    finally:
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_native_cross_source_winners_cards_and_stable_ties_match_the_original_oracle(tmp_path, monkeypatch):
    from captain_hook.tickets import context as source_module

    monkeypatch.setattr(source_module, "board_state", lambda tree, cwd: {})
    payload = {"type": "slack.action", "action_ts": "owner-card", "label": "No", "waits": True,
        "question": "Post to C1?", "options": ["Yes", "No"]}
    tap = f'<channel source="{source_module.CARD_SERVER}" type="slack.action">\n{json.dumps(payload)}\n</channel>'
    root_messages = [T.user("owner C1"), *question("duplicate", "Yes", 1), *question("tie", "Yes", 2),
        *question("overwritten", "Yes", 2), T.user(tap, isMeta=True),
        T.user(tap.replace("owner-card", "tool-card"), isMeta=True, sourceToolUseID="not-owner")]
    acting_messages = [T.user("agent brief"), *question("duplicate", "No", 2),
        T.assistant(T.tool("Bash", id="overwritten", command="echo C1")),
        T.user(tap.replace("owner-card", "agent-card"), isMeta=True)]
    root_path, acting_path = tmp_path / "root.jsonl", tmp_path / "acting.jsonl"
    root_path.write_text(lines(root_messages))
    acting_path.write_text(lines(acting_messages))
    reference = TranscriptSources(event(tmp_path, fixture_session(acting_messages), fixture_session(root_messages), root_path), ("C1",))
    original = [(turn, user) for session, user in reference.sessions() for turn in session.turns]
    asked = {answer.id: answer for use in source_module.ordered_uses(turn for turn, _ in original)
        for answer in source_module.use_answers(use)}
    cards = {answer.id: answer for turn, user in original if user
        for value, at in source_module.card_taps(turn.events)
        if (answer := source_module.card_answer(value, at)) is not None}
    expected = [*asked.values(), *cards.values()]
    assert [answer.id for answer in expected] == ["ask:duplicate#0", "ask:tie#0", "card:owner-card"]
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    acting, root = lazy_transcript(acting_path), root_transcript(root_path, 256)
    try:
        actual = TranscriptSources(event(tmp_path, acting, root, root_path), ("C1",)).answers()
        assert actual == expected
        assert actual[0].chosen == ("No",)
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
    finally:
        root.release()
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_resident_default_loader_declares_the_standard_source_without_reading(tmp_path):
    from captain_hook.worker.runtime import ProductRuntime

    path = tmp_path / "not-created.jsonl"
    runtime = ProductRuntime(install_writer=False)
    acting = lazy_transcript(path, loader=runtime._transcript_loader)
    try:
        assert acting.pins.spec is not None
        assert acting.pins.spec.path == path
        assert acting.pins.spec.tail_events is None
        assert acting.session is None
        assert not path.exists()
    finally:
        acting.release()


def test_selected_acting_tail_preserves_its_existing_scope(tmp_path, monkeypatch):
    from captain_hook.tickets import context as source_module

    monkeypatch.setattr(source_module, "board_state", lambda tree, cwd: {})
    messages = [T.user("first"), *question("older-question"), T.user("recent"),
        T.assistant(T.tool("Bash", id="recent-touch", command="echo C1"))]
    assert any(source_module.use_answers(use) for turn in fixture_session(messages).turns for use in turn.tool_uses)
    path = tmp_path / "acting.jsonl"
    path.write_text(lines(messages))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    acting = lazy_transcript(path, loader=load_transcript)
    selected = acting.fork(2)
    try:
        assert selected.pins.spec is not None and selected.pins.spec.tail_events == 2
        sources = TranscriptSources(event(tmp_path, selected), ("C1",))
        assert sources.answers() == []
        assert [touch.tool for touch in sources.touches()] == ["Bash"]
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
    finally:
        selected.release()
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


@pytest.mark.parametrize("large_bytes", [32, 2 * 1024 * 1024])
def test_native_instruction_evidence_keeps_words_without_successful_tool_payloads(tmp_path, monkeypatch, large_bytes):
    from captain_hook.snapshots.client import DEFAULT_LIMITS

    first = "first offer " + "a" * 430
    last = "last offer " + "z" * 430
    command = T.tool("Bash", id="unused-input", command="x" * large_bytes)
    messages = [
        T.user("repeat", timestamp="2026-01-01T00:00:00Z"),
        T.assistant(f"  {first}  ", command),
        T.user(T.result("success", of=command), timestamp="2026-01-01T00:00:01Z"),
        T.user("repeat", timestamp="2026-01-01T00:00:02Z"),
        T.assistant(f"  {last}  "),
        T.user("repeat", timestamp="2026-01-01T00:00:03Z"),
    ]
    expected = TranscriptSources(event(tmp_path, fixture_session(messages)), ()).said()
    assert expected
    assert all(item.detail == f"the agent's message it answers ends: {last[-400:]}" for item in expected)
    path = tmp_path / "acting.jsonl"
    path.write_text(lines(messages))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    acting = lazy_transcript(path)
    requests = []
    exchange = fixture.client._exchange

    def recorded(wrapper):
        requests.append(wrapper["request"])
        return exchange(wrapper)

    monkeypatch.setattr(fixture.client, "_exchange", recorded)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_output_bytes", 4096)
    try:
        if large_bytes > 4096:
            baseline = fixture.load(path)
            try:
                with pytest.raises(EvidenceIncomplete) as refused:
                    baseline.recent_messages(60).turns
                assert refused.value.status == "output_limit"
            finally:
                baseline.release()
        requests.clear()
        actual = TranscriptSources(event(tmp_path, acting), ()).said()
        assert actual == expected
        queries = [request["query"] for request in requests if request["operation"] == "query"]
        assert queries == [{"kind": "instruction_turns", "order": "forward"}]
        assert fixture.client.call("stats")["data"]["gauges"]["active_leases"] == 0
        print(f"ticket_instruction_source unused_input_bytes={large_bytes} query_count={len(queries)} evidence_count={len(actual)} output_limit=4096")
    finally:
        acting.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()
