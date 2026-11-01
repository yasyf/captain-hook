from __future__ import annotations

import contextvars
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from captain_hook import dispatch_metrics as metrics
from captain_hook import session
from captain_hook.context import HookContext
from captain_hook.types import Event
from captain_hook.worker import runtime as worker_runtime
from tests.test_worker_runtime import FakeRegistry, request


class Value(BaseModel):
    count: int = 0


@pytest.mark.parametrize(
    "event",
    [event.name for event in Event if event.name not in {"UserPromptSubmit", "PreToolUse", "Stop"}] + ["Unknown"],
)
def test_unselected_calls_have_no_timer_or_metric_work(monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    def forbidden() -> float:
        raise AssertionError("an unselected call read the phase clock")

    monkeypatch.setattr(metrics, "perf_counter", forbidden)
    with metrics.collect_dispatch("UserPromptSubmit", 1) as outer:
        with metrics.collect_dispatch(event, 2) as inactive:
            assert inactive is None
            assert metrics.measured_call("handler", "example", lambda value: value + 1, 4) == 5
        assert metrics.ACTIVE.get() is outer
    assert metrics.ACTIVE.get() is None


@pytest.mark.parametrize("elapsed_ms,logged", [(249.9, False), (250.0, True)])
@pytest.mark.parametrize(
    "event,prefix", [("UserPromptSubmit", "submit"), ("PreToolUse", "dispatch"), ("Stop", "dispatch")]
)
def test_summary_only_enriches_slow_selected_events_and_closes_late_work(
    elapsed_ms: float, logged: bool, event: str, prefix: str
) -> None:
    with metrics.collect_dispatch(event, 7) as collected:
        assert collected is not None
        metrics.note_session("session-identity")
        collected.record("handler", "example", 25.0)
        result = collected.summary(elapsed_ms)
        assert (f"{prefix}_phases" in result) is logged
        assert result["request_id"] == 7
        if event == "UserPromptSubmit":
            assert result["session_sha256"] == hashlib.sha256(b"session-identity").hexdigest()
            assert "worker_pid" not in result
        else:
            assert result["worker_pid"] == os.getpid()
            assert "session_sha256" not in result
            assert not any(key.startswith("submit_") for key in result)
        assert "session-identity" not in str(result)
        collected.record("handler", "too-late", 1000.0)
        assert collected.phases["handler"].count == 1
        if logged:
            assert result["request_id"] == 7
            assert result[f"{prefix}_phases_overlap"] is True


@pytest.mark.parametrize(
    "event,prefix", [("UserPromptSubmit", "submit"), ("PreToolUse", "dispatch"), ("Stop", "dispatch")]
)
def test_measured_call_preserves_results_and_exceptions(
    monkeypatch: pytest.MonkeyPatch, event: str, prefix: str
) -> None:
    ticks = iter([1.0, 1.025, 2.0, 2.075])
    monkeypatch.setattr(metrics, "perf_counter", lambda: next(ticks))
    failure = ValueError("original failure")

    def fail() -> None:
        raise failure

    with metrics.collect_dispatch(event, 8) as collected:
        assert metrics.measured_call("handler", "success", lambda *, value: value, value="unchanged") == "unchanged"
        with pytest.raises(ValueError) as raised:
            metrics.measured_call("handler", "failure", fail)
        assert raised.value is failure
        assert collected is not None
        assert collected.summary(300.0)[f"{prefix}_phases"] == {
            "handler": {
                "count": 2,
                "sum_ms": 100.0,
                "max_ms": 75.0,
                "slowest": [{"name": "failure", "ms": 75.0}, {"name": "success", "ms": 25.0}],
            }
        }


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse", "Stop"])
def test_fanout_metrics_keep_only_three_bounded_names(event: str) -> None:
    with metrics.collect_dispatch(event, 9) as collected:
        assert collected is not None
        for i in range(100):
            collected.record("conditions", "x" * 1000, float(i))
        assert collected.phases["conditions"].count == 100
        assert collected.phases["conditions"].sum_ms == 4950.0
        assert collected.phases["conditions"].slowest == [(99.0, "x" * 96), (98.0, "x" * 96), (97.0, "x" * 96)]


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse", "Stop"])
def test_handler_fanout_shares_one_request_collector(event: str) -> None:
    with metrics.collect_dispatch(event, 11) as collected:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, metrics.measured_call, "handler", "example", lambda: None)
                for _ in range(100)
            ]
            for future in futures:
                assert future.result() is None
        assert collected is not None
        assert collected.phases["handler"].count == 100
        assert len(collected.phases["handler"].slowest) == 3
        assert metrics.ACTIVE.get() is collected


@pytest.mark.parametrize("event", ["PreToolUse", "Stop", "UserPromptSubmit", "PostToolUse"])
def test_unchanged_state_observation_adds_no_reads_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    slot = session.SessionSlot(tmp_path, Value)
    slot.set(Value(count=1))
    reads: list[Path] = []
    writes: list[Path] = []
    read_text, write = Path.read_text, session.atomic_write

    def counted_read(path: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(path)
        return read_text(path, *args, **kwargs)

    def counted_write(path: Path, text: str) -> None:
        writes.append(path)
        write(path, text)

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr(session, "atomic_write", counted_write)
    with metrics.collect_dispatch(event, 10) as collected:
        with slot.mutate() as value:
            assert value.count == 1
        assert reads == [tmp_path / "value.json"]
        assert writes == [tmp_path / "value.json"]
        assert (tmp_path / "value.json").read_bytes() == b'{"count":1}'
        if collected is not None:
            assert collected.phases["state_write"].count == 1
            summary = collected.summary(300.0)
            if event == "UserPromptSubmit":
                assert summary["submit_state_writes"] == {"same": 1, "different": 0, "uncompared": 0}
            else:
                assert collected.writes == {"same": 0, "different": 0, "uncompared": 0}
                assert "submit_state_writes" not in summary
                assert "dispatch_state_writes" not in summary


@pytest.mark.parametrize("queue_ms,runtime_ms,slow", [(1.0, 300.0, True), (1000.0, 20.0, True), (1.0, 20.0, False)])
@pytest.mark.parametrize(
    "event,prefix", [("UserPromptSubmit", "submit"), ("PreToolUse", "dispatch"), ("Stop", "dispatch")]
)
def test_selected_metrics_use_worker_receipt_and_one_existing_record(
    monkeypatch: pytest.MonkeyPatch,
    logcap: Any,
    queue_ms: float,
    runtime_ms: float,
    slow: bool,
    event: str,
    prefix: str,
) -> None:
    outer_ticks = iter([100.0, 100.0 + runtime_ms / 1000])
    phase_ticks = iter([1.0, 1.0 + runtime_ms / 10000, 2.0, 2.0 + runtime_ms / 4000])
    monkeypatch.setattr(worker_runtime.time, "perf_counter", lambda: next(outer_ticks))
    monkeypatch.setattr(metrics, "perf_counter", lambda: next(phase_ticks))

    def dispatch(*_: object, **__: object) -> tuple[None, object]:
        metrics.measured_call("conditions", "example", lambda: None)
        return None, lambda: None

    runtime = worker_runtime.ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=dispatch, install_writer=False
    )
    response, _ = runtime.dispatch(
        replace(
            request(event=event, payload_raw='{"session_id":"metric-session"}'),
            received=100.0 - queue_ms / 1000,
        )
    )
    assert response.stdout == "discovered out\n"
    assert response.stderr == "discovered err\n"
    records = [record.message for record in logcap.records if record.message.startswith("dispatch ")]
    assert len(records) == 1
    assert f"elapsed_ms={runtime_ms}" in records[0]
    assert f"queue_ms={queue_ms}" in records[0]
    assert "request_id=1" in records[0]
    if event == "UserPromptSubmit":
        assert hashlib.sha256(b"metric-session").hexdigest() in records[0]
        assert "worker_pid=" not in records[0]
    else:
        assert f"worker_pid={os.getpid()}" in records[0]
        assert "session_sha256" not in records[0]
        assert "submit_" not in records[0]
    assert (f"{prefix}_phases=" in records[0]) is slow
    if slow:
        assert f"{prefix}_worker_ms={queue_ms + runtime_ms}" in records[0]
        assert f"'conditions': {{'count': 1, 'sum_ms': {runtime_ms / 4}" in records[0]
        assert f"'registry': {{'count': 1, 'sum_ms': {runtime_ms / 10}" in records[0]
    assert "metric-session" not in records[0]
    assert metrics.ACTIVE.get() is None


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse", "Stop"])
def test_model_and_decision_phases_preserve_calls_without_logging_text(
    monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    calls: list[str] = []
    decided = object()

    def model(prompt: str, **_: object) -> str:
        calls.append(prompt)
        return "unchanged result"

    monkeypatch.setattr("spawnllm.call_sync", model)
    monkeypatch.setattr("spawnllm.decide_sync", lambda *args, **kwargs: decided)
    ctx = HookContext(session=session.SessionStore(None), transcript=MagicMock(), settings=None)
    with metrics.collect_dispatch(event, 12) as collected:
        assert ctx.call_llm("DO NOT LOG THIS TEXT", evidence=False) == "unchanged result"
        assert ctx.decide("PRIVATE DECISION STATE", {}, provider="jev", timeout=1.0) is decided
        assert calls == ["DO NOT LOG THIS TEXT"]
        assert collected is not None
        assert {phase: item.count for phase, item in collected.phases.items()} == {
            "backend": 1,
            "llm_prepare": 1,
            "llm": 1,
            "decide": 1,
        }
        summary = str(collected.summary(300.0))
        assert "DO NOT LOG THIS TEXT" not in summary
        assert "PRIVATE DECISION STATE" not in summary


@pytest.mark.parametrize("event", ["PreToolUse", "Stop"])
def test_new_selected_events_do_not_hash_session_or_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    slot = session.SessionSlot(tmp_path, Value)
    slot.set(Value(count=1))

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("new selected-event telemetry hashed content")

    monkeypatch.setattr(metrics.hashlib, "sha256", forbidden)
    with metrics.collect_dispatch(event, 13) as collected:
        assert collected is not None
        metrics.note_session("PRIVATE SESSION ID")
        with slot.mutate() as value:
            assert value.count == 1
        summary = collected.summary(300.0)
        assert "session_sha256" not in summary
        assert "PRIVATE SESSION ID" not in str(summary)
        assert collected.writes == {"same": 0, "different": 0, "uncompared": 0}


@pytest.mark.parametrize("event", ["PreToolUse", "Stop"])
def test_new_selected_metrics_have_constant_phase_overhead_and_no_phase_io(
    monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    clocks = 0

    def tick() -> float:
        nonlocal clocks
        clocks += 1
        return clocks / 1000

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("phase telemetry performed I/O or content hashing")

    monkeypatch.setattr(metrics, "perf_counter", tick)
    phases = get_args(metrics.Phase.__value__)
    with monkeypatch.context() as local:
        local.setattr("builtins.open", forbidden)
        local.setattr(Path, "read_text", forbidden)
        local.setattr(Path, "write_text", forbidden)
        local.setattr(metrics.hashlib, "sha256", forbidden)
        with metrics.collect_dispatch(event, 14) as collected:
            assert collected is not None
            for phase in phases:
                for _ in range(100):
                    assert metrics.measured_call(phase, "🤖" * 1000, lambda: "unchanged") == "unchanged"
            summary = collected.summary(300.0)
            assert clocks == len(phases) * 200
            assert metrics.measured_call("handler", "closed", lambda: "still unchanged") == "still unchanged"
            assert clocks == len(phases) * 200
    assert len(collected.phases) == len(phases)
    assert all(item.count == 100 and len(item.slowest) == 3 for item in collected.phases.values())
    assert all(len(name) == 96 for item in collected.phases.values() for _, name in item.slowest)
    assert len(str(summary).encode()) < 20000
    assert summary["dispatch_phases_overlap"] is True
