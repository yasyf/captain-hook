from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger

from captain_hook.app import reset
from captain_hook.decisions import open_decision_log

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore


@pytest.fixture(autouse=True)
def clean_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    # Isolate the on-disk state dir per test: SessionStore and fire-counts (max_fires)
    # root under resolve_state_dir(), which defaults to the real ~/.claude/state.
    # Without this, run_cli subprocesses sharing a stdin session_id share one session
    # dir, so fire-state leaks across tests under random ordering.
    # TestStateRoot monkeypatches CAPTAIN_HOOK_STATE_DIR over this autouse value;
    # composition is safe because the test-local monkeypatch tears down first. The
    # decision ledger defaults to the real ~/.cc-transcript/decisions.db, so it gets
    # its own per-test override (inherited by run_cli subprocesses).
    monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(tmp_path_factory.mktemp("hook-state")))
    monkeypatch.setenv("CAPT_HOOK_DECISIONS_DB", str(tmp_path_factory.mktemp("decisions") / "decisions.db"))
    # The SessionEnd reviewer skips headless entrypoints (sdk-*); scrub it so tests don't
    # inherit the ambient CLAUDE_CODE_ENTRYPOINT of a pytest run launched inside claude.
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    open_decision_log.cache_clear()
    reset()
    yield
    open_decision_log.cache_clear()
    reset()


@dataclass
class LogRecord:
    levelno: int
    message: str
    exc_info: Any


class LogCapture:
    """Collects loguru records for assertions, mirroring the bits of pytest ``caplog`` we use."""

    def __init__(self) -> None:
        self.records: list[LogRecord] = []
        self._lines: list[str] = []

    def sink(self, message: Any) -> None:
        record = message.record
        extra = record["extra"]
        rendered = record["message"]
        if extra:
            rendered += " " + " ".join(f"{k}={v!r}" for k, v in extra.items())
        self.records.append(LogRecord(record["level"].no, rendered, record["exception"]))
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


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ReviewStore]:
    from captain_hook.review.store import ReviewStore

    async with await ReviewStore.open(tmp_path / "review.db") as opened:
        yield opened


@pytest.fixture
def settings(tmp_path: Path) -> ReviewSettings:
    from captain_hook.review.settings import ReviewSettings

    return ReviewSettings(db_path=tmp_path / "review.db")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:yasyf/scratch.git"], check=True)
    return repo


@pytest.fixture
def work_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="src_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def session_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="session_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)
