from __future__ import annotations

import hashlib
import os
import threading
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SLOW_DISPATCH_MS = 250.0
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

ACTIVE: ContextVar[DispatchMetrics | None] = ContextVar("captain_hook_dispatch_metrics", default=None)


@dataclass
class PhaseMetrics:
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0
    slowest: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class DispatchMetrics:
    request_id: int
    event: str
    session_sha256: str | None = None
    phases: defaultdict[Phase, PhaseMetrics] = field(default_factory=lambda: defaultdict(PhaseMetrics))
    writes: dict[WriteComparison, int] = field(default_factory=lambda: {"same": 0, "different": 0, "uncompared": 0})
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
            submit = self.event == "UserPromptSubmit"
            correlation: dict[str, object] = {"request_id": self.request_id}
            if submit:
                correlation["session_sha256"] = self.session_sha256
            else:
                correlation["worker_pid"] = os.getpid()
            if worker_ms < SLOW_DISPATCH_MS:
                return correlation
            prefix = "submit" if submit else "dispatch"
            fields: dict[str, object] = {
                f"{prefix}_worker_ms": round(worker_ms, 1),
                f"{prefix}_phases": {
                    phase: {
                        "count": stats.count,
                        "sum_ms": round(stats.sum_ms, 1),
                        "max_ms": round(stats.max_ms, 1),
                        "slowest": [{"name": name, "ms": round(ms, 1)} for ms, name in stats.slowest],
                    }
                    for phase, stats in sorted(self.phases.items())
                },
            }
            if submit:
                fields["submit_state_writes"] = dict(self.writes)
            fields[f"{prefix}_phases_overlap"] = True
            return correlation | fields


@contextmanager
def collect_dispatch(event: str, request_id: int) -> Iterator[DispatchMetrics | None]:
    metrics = DispatchMetrics(request_id, event) if event in ("UserPromptSubmit", "PreToolUse", "Stop") else None
    token = ACTIVE.set(metrics)
    try:
        yield metrics
    finally:
        ACTIVE.reset(token)


def compare_state_writes() -> bool:
    return (metrics := ACTIVE.get()) is not None and metrics.event == "UserPromptSubmit" and not metrics.closed


def note_session(session_id: str | None) -> None:
    if (metrics := ACTIVE.get()) is not None and metrics.event == "UserPromptSubmit" and session_id is not None:
        metrics.session_sha256 = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()


def note_write(comparison: WriteComparison) -> None:
    if (metrics := ACTIVE.get()) is not None and metrics.event == "UserPromptSubmit":
        metrics.write(comparison)


def measured_call[T, **P](phase: Phase, name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    if (metrics := ACTIVE.get()) is None or metrics.closed:
        return function(*args, **kwargs)
    started = perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        metrics.record(phase, name, (perf_counter() - started) * 1000)
