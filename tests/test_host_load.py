from __future__ import annotations

import pytest

from captain_hook.util import builds
from captain_hook.util.builds import LOAD_PER_CORE, HostLoad


@pytest.mark.parametrize(
    ("idle", "saturated"),
    [
        pytest.param(760, False, id="a-quarter-busy-under-a-half-limit"),
        pytest.param(320, True, id="sixty-percent-busy"),
    ],
)
def test_the_gate_reads_cpu_busy_not_the_load_average(
    idle: int, saturated: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOAD_PER_CORE, "0.5")
    monkeypatch.setattr(builds, "BUSY_SAMPLE_SECONDS", 0)
    monkeypatch.setattr(builds.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(builds.os, "getloadavg", lambda: (200.0, 200.0, 200.0))
    samples = iter([(0, 0), (idle, 800)])
    monkeypatch.setattr(builds, "cpu_ticks", lambda: next(samples))

    assert HostLoad.sample().saturated is saturated


def test_a_sample_with_no_ticks_reads_as_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builds, "BUSY_SAMPLE_SECONDS", 0)
    monkeypatch.setattr(builds, "cpu_ticks", lambda: (100, 800))

    assert builds.busy_fraction() == 0.0


@pytest.mark.parametrize(("limit", "saturated"), [("0", True), ("1000000", False)])
def test_a_limit_that_settles_it_takes_no_sample(limit: str, saturated: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOAD_PER_CORE, limit)
    monkeypatch.setattr(builds, "cpu_ticks", lambda: pytest.fail("sampled the CPU"))

    assert HostLoad.sample().saturated is saturated


def test_cpu_ticks_reads_this_host() -> None:
    idle, total = builds.cpu_ticks()
    assert 0 <= idle <= total
    assert total > 0
