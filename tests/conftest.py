from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import pytest
from loguru import logger

from captain_hook.app import reset


@pytest.fixture(autouse=True)
def clean_state():
    reset()
    yield
    reset()


@dataclass
class _LogRecord:
    levelno: int
    message: str
    exc_info: Any


class LogCapture:
    """Collects loguru records for assertions, mirroring the bits of pytest ``caplog`` we use."""

    def __init__(self) -> None:
        self.records: list[_LogRecord] = []
        self._lines: list[str] = []

    def sink(self, message: Any) -> None:
        record = message.record
        extra = record["extra"]
        rendered = record["message"]
        if extra:
            rendered += " " + " ".join(f"{k}={v!r}" for k, v in extra.items())
        self.records.append(_LogRecord(record["level"].no, rendered, record["exception"]))
        self._lines.append(str(message))

    @property
    def text(self) -> str:
        return "".join(self._lines)


@pytest.fixture
def logcap():
    """Capture captain-hook's loguru output (with truncation patcher active) into a ``LogCapture``."""
    from captain_hook.log import make_format, truncate_bound_values

    cap = LogCapture()
    logger.remove()
    logger.configure(patcher=truncate_bound_values)
    sink_id = logger.add(cap.sink, level="DEBUG", format=make_format("{level} {name}: {message}"))
    yield cap
    logger.remove(sink_id)


@pytest.fixture
def isolate_modules():
    snapshot_modules = set(sys.modules.keys())
    snapshot_path = sys.path[:]
    yield
    for key in set(sys.modules.keys()) - snapshot_modules:
        del sys.modules[key]
    sys.path[:] = snapshot_path
