"""Paired-window measurement: one execve count and one wall time per sample.

The kernel counter :mod:`bench.counter` reads is system-wide, so a window around a
command also counts whatever else the machine execs while it is open. Every sample
therefore pairs its command window with an idle window of the same length, and both
estimators are the minimum over the samples, because both contaminants — a neighbour
process execing, and the endpoint-security toll this machine charges every exec —
only ever add.

The minimum equals the true count exactly when some window caught the machine idle,
so how often the idle windows came back empty is what says whether it did. That
fraction is the per-window chance of a clean catch; over N samples the chance of
having missed every one of them is ``(1 - fraction) ** N``, and a count is reported
only once that is under one percent and the minimum has repeated. Sampling runs until
it is, or until the budget expires — and then the run says it could not count rather
than reporting a floor that is really an upper bound. Latency is reported either way:
a busy machine inflates it, and the minimum says so by staying where it is.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from statistics import median

from bench.counter import execs

CONFIDENCE = 0.99
CONFIRMATIONS = 3
MINIMUM_SAMPLES = 30
MAXIMUM_SAMPLES = 20_000
BUDGET_S = 20.0


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    stdin: bytes = b""
    env: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Window:
    execs: int
    ms: float


@dataclass(frozen=True, slots=True)
class Measurement:
    label: str
    active: tuple[Window, ...]
    idle: tuple[Window, ...]

    @property
    def floor(self) -> int:
        return min(window.execs for window in self.active)

    @property
    def confirmations(self) -> int:
        return sum(window.execs == self.floor for window in self.active)

    @property
    def quiet_fraction(self) -> float:
        return sum(window.execs == 0 for window in self.idle) / len(self.idle)

    @property
    def confidence(self) -> float:
        return 1 - (1 - self.quiet_fraction) ** len(self.active)

    @property
    def counted(self) -> bool:
        return self.confidence >= CONFIDENCE and self.confirmations >= CONFIRMATIONS

    @property
    def execs(self) -> int | None:
        return self.floor if self.counted else None

    @property
    def ms_min(self) -> float:
        return min(window.ms for window in self.active)

    @property
    def ms_median(self) -> float:
        return median(window.ms for window in self.active)

    @property
    def ms_p90(self) -> float:
        return percentile(sorted(window.ms for window in self.active), 0.9)


class DispatchFailed(RuntimeError):
    """A measured command exited nonzero, so the sample measures a failure."""


def percentile(ordered: Sequence[float], fraction: float) -> float:
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def timed(action: Callable[[], object]) -> Window:
    before = execs()
    started = time.perf_counter_ns()
    action()
    elapsed = time.perf_counter_ns() - started
    return Window(execs() - before, elapsed / 1e6)


@contextmanager
def spawner(command: Command) -> Iterator[Callable[[], None]]:
    with tempfile.TemporaryFile() as stdin, open(os.devnull, "wb") as sink:
        stdin.write(command.stdin)
        stdin.flush()
        yield lambda: spawn(command, stdin.fileno(), sink.fileno())


def spawn(command: Command, stdin: int, sink: int) -> None:
    os.lseek(stdin, 0, os.SEEK_SET)
    pid = os.posix_spawn(
        command.argv[0],
        list(command.argv),
        command.env if command.env is not None else os.environ,
        file_actions=[
            (os.POSIX_SPAWN_DUP2, stdin, 0),
            (os.POSIX_SPAWN_DUP2, sink, 1),
            (os.POSIX_SPAWN_DUP2, sink, 2),
        ],
    )
    if code := os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]):
        raise DispatchFailed(f"{command.argv[0]} exited {code}")


def pair(run: Callable[[], None]) -> tuple[Window, Window]:
    active = timed(run)
    return active, timed(lambda: time.sleep(active.ms / 1000))


def measure(
    label: str,
    command: Command,
    *,
    budget_s: float = BUDGET_S,
    minimum_samples: int = MINIMUM_SAMPLES,
    maximum_samples: int = MAXIMUM_SAMPLES,
) -> Measurement:
    """Sample ``command`` until its exec count is confidently counted, or until the budget runs out."""
    deadline = time.monotonic() + budget_s
    with spawner(command) as run:
        run()
        pairs = [pair(run) for _ in range(minimum_samples)]
        while len(pairs) < maximum_samples and time.monotonic() < deadline:
            if measurement(label, pairs).counted:
                break
            pairs.append(pair(run))
    return measurement(label, pairs)


def measurement(label: str, pairs: Sequence[tuple[Window, Window]]) -> Measurement:
    return Measurement(label, tuple(active for active, _ in pairs), tuple(idle for _, idle in pairs))
