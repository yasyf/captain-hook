"""The benchmark harness's own red line.

A harness that miscounts would prove a refactor that never happened, so it is graded
against exec counts verified by hand: ``posix_spawn`` of a binary is one ``execve``,
``env`` adds one more for the command it replaces itself with, and a shell adds one
for itself plus one for each command it runs. ``/bin/bash`` is the shell here rather
than ``/bin/sh``, which costs two — Apple's ``sh`` re-execs itself into POSIX mode, so
its count is right but no longer obvious, and a case nobody can verify by eye grades
nothing.
"""

from __future__ import annotations

import sys

import pytest

from bench.measure import Command, Measurement, Window, measure

BUDGET_S = 60.0

# The counter is a Darwin sysctl; there is no unprivileged equivalent to fall back to.
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS exec counter")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(("/usr/bin/true",), 1, id="one-binary"),
        pytest.param(("/usr/bin/env", "/usr/bin/true"), 2, id="env-replaces-itself"),
        pytest.param(("/usr/bin/env", "/usr/bin/env", "/usr/bin/true"), 3, id="two-links"),
        pytest.param(("/bin/bash", "-c", "/usr/bin/true"), 2, id="shell-and-one-child"),
        pytest.param(("/bin/bash", "-c", "/usr/bin/true; /usr/bin/true"), 3, id="shell-and-two-children"),
    ],
)
def test_harness_counts_execs_correctly(argv: tuple[str, ...], expected: int) -> None:
    measurement = measure("counted", Command(argv), budget_s=BUDGET_S)
    assert measurement.execs == expected, (
        f"floor {measurement.floor} confirmed {measurement.confirmations}x at confidence "
        f"{measurement.confidence:.3f} over {len(measurement.active)} samples"
    )


def test_harness_detects_a_planted_regression() -> None:
    clean = measure("clean", Command(("/bin/bash", "-c", "/usr/bin/true")), budget_s=BUDGET_S)
    extra = measure("extra-exec", Command(("/bin/bash", "-c", "/usr/bin/true; /usr/bin/true")), budget_s=BUDGET_S)
    planted = measure("planted", Command(("/bin/bash", "-c", "/bin/sleep 0.005; /usr/bin/true")), budget_s=BUDGET_S)
    assert clean.execs == 2
    assert extra.execs == clean.execs + 1
    assert planted.execs == clean.execs + 1
    assert planted.ms_min - extra.ms_min >= 3


def test_harness_refuses_a_count_no_quiet_window_confirmed() -> None:
    floor = (Window(3, 1.0),) * 5
    assert Measurement("never-quiet", floor, (Window(1, 1.0),) * 5).execs is None
    assert Measurement("lone-floor", (Window(3, 1.0), *(Window(9, 1.0),) * 4), (Window(0, 1.0),) * 5).execs is None
    assert Measurement("counted", floor, (Window(0, 1.0),) * 5).execs == 3
