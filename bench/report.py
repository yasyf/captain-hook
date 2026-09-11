"""The recorded baseline at ``bench/baseline.json`` — the numbers a later run is graded against.

A baseline is data, not prose: a refactor that claims to drop an exec or shave a
dispatch is checked by re-running ``python -m bench.cli run`` and diffing this file,
not by remembering what the last measurement said. Latency travels with the machine
facts that set its scale, because the same chain on the same machine costs what the
endpoint-security exec toll of the hour charges it.
"""

from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bench.counter import endpoint_security_toll_us
from bench.measure import Measurement

BASELINE = Path(__file__).resolve().parent / "baseline.json"


@dataclass(frozen=True, slots=True)
class Record:
    label: str
    execs: int | None
    exec_floor: int
    confirmations: int
    confidence: float
    quiet_fraction: float
    samples: int
    ms_min: float
    ms_median: float
    ms_p90: float


def record(measurement: Measurement) -> Record:
    return Record(
        label=measurement.label,
        execs=measurement.execs,
        exec_floor=measurement.floor,
        confirmations=measurement.confirmations,
        confidence=round(measurement.confidence, 4),
        quiet_fraction=round(measurement.quiet_fraction, 4),
        samples=len(measurement.active),
        ms_min=round(measurement.ms_min, 2),
        ms_median=round(measurement.ms_median, 2),
        ms_p90=round(measurement.ms_p90, 2),
    )


def machine(build: str) -> dict[str, object]:
    return {
        "recorded": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build": build,
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "endpoint_security_toll_us": round(endpoint_security_toll_us(), 1),
    }


def write_baseline(records: list[Record], *, build: str) -> Path:
    BASELINE.write_text(
        json.dumps({"machine": machine(build), "records": [asdict(r) for r in records]}, indent=2) + "\n"
    )
    return BASELINE


def table(records: list[Record]) -> str:
    rows = [
        f"| {r.label} | {r.execs if r.execs is not None else f'>={r.exec_floor} (uncounted)'} | "
        f"{r.ms_min} | {r.ms_median} | {r.ms_p90} | {r.samples} | {r.confidence} |"
        for r in records
    ]
    return "\n".join(
        [
            "| scenario | execs/event | ms min | ms median | ms p90 | samples | confidence |",
            "|---|---|---|---|---|---|---|",
            *rows,
        ]
    )
