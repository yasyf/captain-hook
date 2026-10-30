from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

USER_SUBMIT_SLOW_MS = 250.0
TOP_NAMES = 3
MAX_NAME = 96

type Phase = Literal[
    "registry",
    "mandatory",
    "prepare",
    "conditions",
    "handler",
    "backend",
    "llm_prepare",
    "llm",
    "decide",
    "state_write",
    "cli",
]
type WriteComparison = Literal["same", "different", "uncompared"]

ACTIVE: ContextVar[SubmitMetrics | None] = ContextVar("captain_hook_submit_metrics", default=None)


@dataclass
class PhaseMetrics:
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0
    slowest: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class SubmitMetrics:
    request_id: int
    session_sha256: str | None = None
    phases: defaultdict[Phase, PhaseMetrics] = field(default_factory=lambda: defaultdict(PhaseMetrics))
    writes: dict[WriteComparison, int] = field(
        default_factory=lambda: {"same": 0, "different": 0, "uncompared": 0}
    )
    closed: bool = False
    guard: threading.Lock = field(default_factory=threading.Lock)

    def record(self, phase: Phase, name: str, elapsed_ms: float) -> None:
        with self.guard:
            if self.closed:
                return
            stats = self.phases[phase]
            stats.count += 1
            stats.sum_ms += elapsed_ms
            stats.max_ms = max(stats.max_ms, elapsed_ms)
            if name:
                stats.slowest = sorted((*stats.slowest, (elapsed_ms, name[:MAX_NAME])), reverse=True)[:TOP_NAMES]

    def write(self, comparison: WriteComparison) -> None:
        with self.guard:
            if not self.closed:
                self.writes[comparison] += 1

    def summary(self, worker_ms: float) -> dict[str, object]:
        with self.guard:
            self.closed = True
            correlation = {"request_id": self.request_id, "session_sha256": self.session_sha256}
            if worker_ms < USER_SUBMIT_SLOW_MS:
                return correlation
            return correlation | {
                "submit_worker_ms": round(worker_ms, 1),
                "submit_phases": {
                    phase: {
                        "count": stats.count,
                        "sum_ms": round(stats.sum_ms, 1),
                        "max_ms": round(stats.max_ms, 1),
                        "slowest": [{"name": name, "ms": round(ms, 1)} for ms, name in stats.slowest],
                    }
                    for phase, stats in sorted(self.phases.items())
                },
                "submit_state_writes": dict(self.writes),
                "submit_phases_overlap": True,
            }


@contextmanager
def collect_submit(event: str, request_id: int) -> Iterator[SubmitMetrics | None]:
    metrics = SubmitMetrics(request_id) if event == "UserPromptSubmit" else None
    token = ACTIVE.set(metrics)
    try:
        yield metrics
    finally:
        ACTIVE.reset(token)


def active() -> bool:
    return (metrics := ACTIVE.get()) is not None and not metrics.closed


def note_session(session_id: str | None) -> None:
    if (metrics := ACTIVE.get()) is not None and session_id is not None:
        metrics.session_sha256 = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()


def note_write(comparison: WriteComparison) -> None:
    if (metrics := ACTIVE.get()) is not None:
        metrics.write(comparison)


def measured_call[T, **P](
    phase: Phase, name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    if (metrics := ACTIVE.get()) is None or metrics.closed:
        return function(*args, **kwargs)
    started = perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        metrics.record(phase, name, (perf_counter() - started) * 1000)
