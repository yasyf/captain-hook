from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from captain_hook.snapshots.client import CORE_SCHEMA, HOST_SCHEMA
from captain_hook.testing.snapshots import FixtureOwner


def user(index: int) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "uuid": f"u{index}",
                "sessionId": "s",
                "timestamp": "2026-01-02T03:04:05Z",
                "message": {"content": f"hello {index}"},
            }
        )
        + "\n"
    )


@pytest.fixture
def owner() -> Iterator[FixtureOwner]:
    fixture = FixtureOwner()
    try:
        yield fixture
    finally:
        fixture.close()


def active_leases(owner: FixtureOwner) -> int:
    reply = owner.exchange(
        {
            "schema": HOST_SCHEMA,
            "tool_registry": [],
            "request": {"schema": CORE_SCHEMA, "id": "stats", "operation": "stats"},
        }
    )
    return reply["response"]["data"]["gauges"]["active_leases"]


def test_review_events_page_a_registered_source_past_its_watermark_without_holding_a_lease(
    tmp_path: Path, owner: FixtureOwner
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(user(0) + user(1))
    first = owner.client.review_events(path, watermark=None, context_events=0, max_pages=4)
    assert (first.positions, first.fresh_start, first.caught_up) == ((0, 1), 0, True)
    assert first.canonical_path == str(path.resolve())
    assert first.mtime_ns == path.stat().st_mtime_ns
    assert active_leases(owner) == 0

    with path.open("a") as handle:
        handle.write(user(2))
    second = owner.client.review_events(path, watermark=first.watermark, context_events=1, max_pages=4)
    assert (second.positions, second.fresh_start, second.caught_up) == ((1, 2), 2, True)
    assert second.source_revision != first.source_revision
    assert active_leases(owner) == 0


def test_review_events_page_lookback_wider_than_one_page_without_a_gap(tmp_path: Path, owner: FixtureOwner) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(user(index) for index in range(300)))
    first = owner.client.review_events(path, watermark=None, context_events=0, max_pages=4)
    assert (first.positions[-1], first.caught_up) == (299, True)

    with path.open("a") as handle:
        handle.write(user(300))
    second = owner.client.review_events(path, watermark=first.watermark, context_events=300, max_pages=1)
    assert (second.positions, second.fresh_start, second.caught_up) == (tuple(range(301)), 300, True)
