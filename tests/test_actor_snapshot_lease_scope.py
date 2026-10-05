from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import pytest

from captain_hook.snapshots import client as snapshots
from captain_hook.snapshots.client import GraphEvidenceExpired, GraphSources, RegisteredWarmState, client_scope
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.transcripts import load_transcript
from tests.helpers import raw_text
from tests.test_snapshot_fixture_owner import write_messages


class FixtureBridge:
    def __init__(self, fixture: FixtureOwner, closed: list[int]) -> None:
        self.fixture = fixture
        self.closed = closed

    def __call__(self, request: dict[str, object]) -> dict[str, object]:
        return self.fixture.exchange(request)

    def close(self) -> None:
        self.closed.append(active_leases(self.fixture))


def active_leases(fixture: FixtureOwner) -> int:
    return fixture.client.call("stats")["data"]["gauges"]["active_leases"]


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    write_messages(source, raw_text("user", "root"))
    attachments = tuple(tmp_path / f"attachment-{index}.jsonl" for index in range(2))
    for path in attachments:
        write_messages(path, raw_text("user", "attached"))
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    sources = GraphSources(direct_paths=attachments)
    warmer = RegisteredWarmState(sources, fixture.client)
    fixture.context["work_class"] = "background"
    for _ in range(32):
        if warmer.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3):
            break
    else:
        pytest.fail("registered fixture did not finish warming")
    fixture.context["work_class"] = "foreground"
    closed: list[int] = []
    monkeypatch.setattr(snapshots, "Bridge", lambda: FixtureBridge(fixture, closed))
    try:
        yield fixture, source, sources, closed
    finally:
        fixture.close()


def test_an_immediate_event_scope_expires_the_graph_a_later_branch_borrows(owner) -> None:
    fixture, source, sources, closed = owner
    baseline = active_leases(fixture)
    with client_scope() as client:
        client.bind_tool_registry({})
        session = load_transcript(source).with_registered_sources(sources)
        first, second = session.retain(), session.retain()
        assert first.has_edit_to("src/**") is False
        first.release()
        with pytest.raises(GraphEvidenceExpired) as expired:
            second.has_edit_to("src/**")
        assert (expired.value.status, expired.value.reason) == (
            "stale_handle",
            "lease does not belong to this claimant or generation",
        )
        second.release()
        session.release()
    assert closed == [baseline]


@pytest.mark.parametrize(
    "interruption",
    [None, RuntimeError, TimeoutError, asyncio.CancelledError, KeyboardInterrupt],
    ids=["completed", "failed", "timed-out", "cancelled", "interrupted"],
)
def test_an_actor_event_scope_keeps_the_borrowed_graph_until_it_closes(owner, interruption) -> None:
    fixture, source, sources, closed = owner
    baseline = active_leases(fixture)
    intruder = fixture.client_for_context(claimant="intruder")
    with pytest.raises(interruption, match="synthetic interruption") if interruption else nullcontext():
        with client_scope(defer_cleanup=True) as client:
            client.bind_tool_registry({})
            session = load_transcript(source).with_registered_sources(sources)
            graph = session.graph
            first, second = session.retain(), session.retain()
            try:
                assert first.has_edit_to("src/**") is False
                first.release()
                assert second.has_edit_to("src/**") is False
                assert second.has_read("README.md") is False
                assert intruder.call("retain", handle=second.lease.handle)["status"] == "stale_handle"
                assert active_leases(fixture) > baseline
                if interruption is not None:
                    raise interruption("synthetic interruption")
            finally:
                second.release()
                session.release()
    assert closed == [baseline]
    assert graph.native_released
    assert not client._graphs
    assert not client._leases
