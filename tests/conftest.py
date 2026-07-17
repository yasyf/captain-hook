from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger

from captain_hook.app import reset
from captain_hook.decisions import open_decision_log
from captain_hook.durable import DurableStore
from captain_hook.review.repo import resolve_repo_key
from captain_hook.session import SessionStore
from captain_hook.util.http import github_token
from captain_hook.util.model_cache import model_sha256, model_version
from captain_hook.util.proc import _cold_skip_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    # Isolate the daemon run dir (sockets, locks, worker meta) per test so no test touches the real
    # ~/.cache/captain-hook/run. Daemon/client tests override this with a short /tmp dir (macOS
    # sun_path); that test-local monkeypatch tears down first, so composition is safe.
    monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(tmp_path_factory.mktemp("run")))
    # The SessionEnd reviewer skips headless entrypoints (sdk-*); scrub it so tests don't
    # inherit the ambient CLAUDE_CODE_ENTRYPOINT of a pytest run launched inside claude.
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    # Isolate CLAUDE_CONFIG_DIR to an empty config dir. Leaving plugins/installed_plugins.json absent
    # is the existence gate that keeps plugin discovery hermetic — enabled_plugins() returns () without
    # ever spawning a real `claude plugin list`. A test that needs discovery plants the file itself.
    config_dir = tmp_path_factory.mktemp("claude-config")
    (config_dir / "plugins").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def clear_global_caches():
    caches = (
        open_decision_log,
        github_token,
        model_version,
        model_sha256,
        resolve_repo_key,
        _cold_skip_permissions,
    )
    for cached in caches:
        cached.cache_clear()
    yield
    for cached in caches:
        cached.cache_clear()


@pytest.fixture(autouse=True)
def isolate_tracked_models():
    session_tracked, durable_tracked = SessionStore.TRACKED[:], DurableStore.TRACKED[:]
    yield
    SessionStore.TRACKED[:] = session_tracked
    DurableStore.TRACKED[:] = durable_tracked


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
    removed = set(sys.modules.keys()) - snapshot_modules
    for key in removed:
        del sys.modules[key]
    sys.path[:] = snapshot_path
    # Removing a submodule leaves the root package's PEP 562 __getattr__ cache pinned to the
    # orphaned object; drop cached exports sourced from a removed module so they re-resolve.
    if (root := sys.modules.get("captain_hook")) is not None:
        for name, target in root._EXPORTS.items():
            if target in removed:
                root.__dict__.pop(name, None)


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
def work_dir(tmp_path: Path) -> Path:
    (d := tmp_path / "src").mkdir()
    return d


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    (d := tmp_path / "session").mkdir()
    return d
