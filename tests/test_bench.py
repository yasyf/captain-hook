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
import time

import pytest

from bench.measure import Command, Measurement, Window, measure, spawn

BUDGET_S = 60.0

# The counter is a Darwin sysctl; there is no unprivileged equivalent to fall back to.
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS exec counter")


def spanned(monkeypatch: pytest.MonkeyPatch, label: str, argv: tuple[str, ...]) -> tuple[Measurement, list[float]]:
    spans: list[float] = []

    def timed_spawn(command: Command, stdin: int, sink: int) -> None:
        started = time.perf_counter_ns()
        spawn(command, stdin, sink)
        spans.append((time.perf_counter_ns() - started) / 1e6)

    monkeypatch.setattr("bench.measure.spawn", timed_spawn)
    return measure(label, Command(argv), budget_s=BUDGET_S), spans


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


def test_harness_detects_a_planted_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    clean, clean_spans = spanned(monkeypatch, "clean", ("/bin/bash", "-c", "/usr/bin/true"))
    extra, extra_spans = spanned(monkeypatch, "extra-exec", ("/bin/bash", "-c", "/usr/bin/true; /usr/bin/true"))
    planted, planted_spans = spanned(monkeypatch, "planted", ("/bin/bash", "-c", "/bin/sleep 0.005; /usr/bin/true"))
    assert clean.execs == 2
    assert extra.execs == clean.execs + 1
    assert planted.execs == clean.execs + 1
    for measurement, spans in ((clean, clean_spans), (extra, extra_spans), (planted, planted_spans)):
        assert len(spans[1:]) == len(measurement.active)
        assert not [
            (window.ms, span) for window, span in zip(measurement.active, spans[1:], strict=True) if window.ms < span
        ]
    assert not [span for span in planted_spans if span < 5]


def test_harness_refuses_a_count_no_quiet_window_confirmed() -> None:
    floor = (Window(3, 1.0),) * 5
    assert Measurement("never-quiet", floor, (Window(1, 1.0),) * 5).execs is None
    assert Measurement("lone-floor", (Window(3, 1.0), *(Window(9, 1.0),) * 4), (Window(0, 1.0),) * 5).execs is None
    assert Measurement("counted", floor, (Window(0, 1.0),) * 5).execs == 3
