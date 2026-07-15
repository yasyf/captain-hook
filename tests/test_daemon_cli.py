from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from capt_hook_client.key import PROTOCOL, worker_key
from captain_hook.cli import cli
from captain_hook.daemon import ops

if TYPE_CHECKING:
    from collections.abc import Iterator


def make_project(root: Path) -> Path:
    (hooks := root / ".claude" / "hooks").mkdir(parents=True)
    (hooks / "__init__.py").write_text("")
    return root


def daemon_env(root: Path, run: Path, base: Path) -> dict[str, str]:
    dirs = {name: base / name for name in ("state", "cache", "logs", "decisions")}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return os.environ | {
        "CAPT_HOOK_RUN_DIR": str(run),
        "CAPTAIN_HOOK_STATE_DIR": str(dirs["state"]),
        "XDG_CACHE_HOME": str(dirs["cache"]),
        "CAPTAIN_HOOK_LOG_DIR": str(dirs["logs"]),
        "CAPT_HOOK_DECISIONS_DB": str(dirs["decisions"] / "d.db"),
        "CLAUDE_PROJECT_DIR": str(root),
    }


def spawn(root: Path, env: dict[str, str], boot_log: Path) -> subprocess.Popen:
    handle = boot_log.open("wb")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "captain_hook", "daemon", "run", "--root", str(root)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
        )
    finally:
        handle.close()


def wait_ready(sock_path: str, proc: subprocess.Popen, boot_log: Path, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited early ({proc.returncode}); boot log:\n{boot_log.read_text()}")
        if os.path.exists(sock_path) and ops.send_control(sock_path, "ping"):
            return
        time.sleep(0.05)
    raise RuntimeError(f"daemon never became ready; boot log:\n{boot_log.read_text()}")


def short_run() -> Path:
    # macOS sun_path caps a socket path at 104 bytes; the run dir must stay short.
    run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chcli-"))
    assert len(str(run)) < 70
    return run


def reaped_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def write_meta(run: Path, key: str, root: Path, pid: int) -> None:
    (run / f"{key}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "root": str(root),
                "build": "stale-build",
                "version": "0.0.0",
                "protocol": PROTOCOL,
                "socket": str(run / f"{key}.sock"),
                "started_at": time.time() - 5,
            }
        )
    )


def write_meta_with_start(run: Path, key: str, root: Path, pid: int, proc_start: str | None) -> None:
    (run / f"{key}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "root": str(root),
                "build": "stale-build",
                "version": "0.0.0",
                "protocol": PROTOCOL,
                "socket": str(run / f"{key}.sock"),
                "started_at": time.time() - 5,
                "proc_start": proc_start,
            }
        )
    )


@pytest.fixture
def live_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path, str, subprocess.Popen]]:
    run = short_run()
    base = tmp_path / "base"
    root = make_project(tmp_path / "proj")
    env = daemon_env(root, run, base)
    sock_path = str(run / f"{worker_key(str(root), env)}.sock")
    boot_log = run / "boot.log"
    proc = spawn(root, env, boot_log)
    # Point the in-process ops CLI at the same run/log dirs the subprocess worker uses (over the
    # per-test conftest run dir); the worker is matched by its recorded root, not by a recomputed key.
    monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
    monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(base / "logs"))
    try:
        wait_ready(sock_path, proc, boot_log)
        yield root, run, sock_path, proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                ops.send_control(sock_path, "shutdown")
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(run, ignore_errors=True)


class TestStatus:
    def test_reports_a_live_worker_alive_and_a_dead_one_stale(
        self, live_worker: tuple[Path, Path, str, subprocess.Popen]
    ) -> None:
        root, run, _, proc = live_worker
        write_meta(run, "deadbeefdeadbeef", root / "gone", pid=reaped_pid())
        result = CliRunner().invoke(cli, ["daemon", "status", "--all", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) == 2
        alive = [r for r in rows if r["alive"]]
        stale = [r for r in rows if not r["alive"]]
        assert [r["pid"] for r in alive] == [proc.pid]
        assert alive[0]["uptime_s"] is not None
        assert len(stale) == 1 and stale[0]["uptime_s"] is None

    def test_human_table_lists_the_worker(self, live_worker: tuple[Path, Path, str, subprocess.Popen]) -> None:
        root, _, _, proc = live_worker
        result = CliRunner().invoke(cli, ["daemon", "status", "--all"])
        assert result.exit_code == 0, result.output
        assert "ROOT" in result.output and "STATE" in result.output
        assert str(proc.pid) in result.output and "alive" in result.output

    def test_root_filter_matches_by_recorded_root(self, live_worker: tuple[Path, Path, str, subprocess.Popen]) -> None:
        root, run, _, proc = live_worker
        write_meta(run, "cafecafecafecafe", root / "other", pid=reaped_pid())
        result = CliRunner().invoke(cli, ["daemon", "status", "--root", str(root), "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert [r["pid"] for r in rows] == [proc.pid]


class TestStop:
    def test_shuts_a_live_worker_down_and_cleans_its_files(
        self, live_worker: tuple[Path, Path, str, subprocess.Popen]
    ) -> None:
        _, run, sock_path, proc = live_worker
        result = CliRunner().invoke(cli, ["daemon", "stop", "--all"])
        assert result.exit_code == 0, result.output
        assert "stopped" in result.output
        assert proc.wait(timeout=5) == 0
        assert not os.path.exists(sock_path)
        assert list(run.glob("*.json")) == []
        follow_up = CliRunner().invoke(cli, ["daemon", "status", "--all"])
        assert "No daemon workers." in follow_up.output

    def test_cleans_a_dead_worker_stale_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = short_run()
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        try:
            write_meta(run, "0011223344556677", tmp_path / "proj", pid=reaped_pid())
            result = CliRunner().invoke(cli, ["daemon", "stop", "--all"])
            assert result.exit_code == 0, result.output
            assert "cleaned" in result.output
            assert list(run.glob("*.json")) == []
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def test_cleans_a_recycled_pid_whose_start_time_mismatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FINDER-8: a meta at a LIVE pid whose recorded start-time no longer matches was recycled to
        # an unrelated process; its files are stale and cleanable, never reported "unreachable".
        run = short_run()
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        try:
            write_meta_with_start(run, "aabbccddeeff0011", tmp_path / "proj", pid=os.getpid(), proc_start="stale")
            result = CliRunner().invoke(cli, ["daemon", "stop", "--all"])
            assert result.exit_code == 0, result.output
            assert "cleaned" in result.output
            assert list(run.glob("*.json")) == []
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def test_leaves_a_live_worker_when_start_time_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # V10: process_start_time returning None (ps timed out/transient) must NOT read as a mismatch that
        # cleans a LIVE worker's files — unknown != stale; fall back to pid_alive and report unreachable.
        from captain_hook.daemon import ops

        run = short_run()
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        monkeypatch.setattr(ops, "process_start_time", lambda _pid: None)
        try:
            write_meta_with_start(
                run, "aabbccddeeff0033", tmp_path / "proj", pid=os.getpid(), proc_start="recorded-start"
            )
            result = CliRunner().invoke(cli, ["daemon", "stop", "--all"])
            assert result.exit_code == 0, result.output
            assert "unreachable" in result.output
            assert list(run.glob("*.json")) != [], "a live worker's files were cleaned on an unknown start-time"
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def test_leaves_a_live_matching_worker_unreachable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # FINDER-8: a live pid whose recorded start-time still matches is a genuinely-running daemon
        # (just unresponsive on its socket) — reported "unreachable", its files kept, not cleaned.
        from captain_hook.util.proc import process_start_time

        run = short_run()
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        try:
            write_meta_with_start(
                run, "aabbccddeeff0022", tmp_path / "proj", pid=os.getpid(), proc_start=process_start_time(os.getpid())
            )
            result = CliRunner().invoke(cli, ["daemon", "stop", "--all"])
            assert result.exit_code == 0, result.output
            assert "unreachable" in result.output
            assert list(run.glob("*.json")) != []
        finally:
            shutil.rmtree(run, ignore_errors=True)


class TestRestart:
    def test_drains_the_worker_so_it_exits(self, live_worker: tuple[Path, Path, str, subprocess.Popen]) -> None:
        root, _, sock_path, proc = live_worker
        result = CliRunner().invoke(cli, ["daemon", "restart", "--root", str(root)])
        assert result.exit_code == 0, result.output
        assert "draining" in result.output
        assert proc.wait(timeout=5) == 0
        deadline = time.monotonic() + 5
        while os.path.exists(sock_path) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not os.path.exists(sock_path)


class TestLogs:
    def test_tails_the_boot_and_daemon_logs(self, live_worker: tuple[Path, Path, str, subprocess.Popen]) -> None:
        root, _, _, _ = live_worker
        result = CliRunner().invoke(cli, ["daemon", "logs", "--root", str(root)])
        assert result.exit_code == 0, result.output
        assert "== boot log:" in result.output
        assert "== daemon log:" in result.output
        assert "daemon up" in result.output  # the worker's startup line, tailed from daemon-<key>.log

    def test_tail_limits_line_count(self, live_worker: tuple[Path, Path, str, subprocess.Popen]) -> None:
        root, run, _, _ = live_worker
        # Force a known key/log by matching the live worker's recorded meta, then tail=1.
        result = CliRunner().invoke(cli, ["daemon", "logs", "--root", str(root), "--tail", "1"])
        assert result.exit_code == 0, result.output
        assert "== daemon log:" in result.output


class TestNoSpawn:
    def test_status_stop_logs_restart_never_spawn_a_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = short_run()
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path / "logs"))
        root = make_project(tmp_path / "proj")
        try:
            for argv in (
                ["daemon", "status", "--all"],
                ["daemon", "stop", "--all"],
                ["daemon", "logs", "--root", str(root)],
                ["daemon", "restart", "--root", str(root)],
            ):
                result = CliRunner().invoke(cli, argv)
                assert result.exit_code == 0, (argv, result.output)
                assert "No daemon workers." in result.output
            assert list(run.glob("*.sock")) == []
            assert list(run.glob("*.json")) == []
        finally:
            shutil.rmtree(run, ignore_errors=True)


class TestWorkerProcessLive:
    def _worker(self, pid: int) -> ops.Worker:
        return ops.Worker(
            key="k",
            pid=pid,
            root="/r",
            build="b",
            version="1",
            socket="/s.sock",
            started_at=0.0,
            proc_start="recorded-start",
        )

    @pytest.mark.parametrize(
        ("forced", "expected"),
        [(None, True), ("recorded-start", True), ("other-start", False)],
        ids=["unknown-is-live", "match-is-live", "mismatch-is-stale"],
    )
    def test_start_time_states(self, monkeypatch: pytest.MonkeyPatch, forced: str | None, expected: bool) -> None:
        # V10: MATCH → live; MISMATCH (both known, differ) → stale; UNKNOWN (None) → live, never cleaned.
        monkeypatch.setattr(ops, "process_start_time", lambda _pid: forced)
        assert ops.worker_process_live(self._worker(os.getpid())) is expected

    def test_dead_pid_is_not_live(self) -> None:
        assert ops.worker_process_live(self._worker(reaped_pid())) is False


class TestOpsHelpers:
    def test_from_meta_rejects_unparseable_and_ill_shaped(self, tmp_path: Path) -> None:
        (garbage := tmp_path / "a.json").write_text("not json")
        assert ops.Worker.from_meta(garbage) is None
        (partial := tmp_path / "b.json").write_text(json.dumps({"pid": "x", "root": "/r"}))
        assert ops.Worker.from_meta(partial) is None

    def test_from_meta_parses_a_valid_meta(self, tmp_path: Path) -> None:
        (path := tmp_path / "abc123.json").write_text(
            json.dumps(
                {
                    "pid": 7,
                    "root": "/r",
                    "build": "b",
                    "version": "1",
                    "protocol": PROTOCOL,
                    "socket": "/s.sock",
                    "started_at": 10.0,
                }
            )
        )
        worker = ops.Worker.from_meta(path)
        assert worker is not None
        assert (worker.key, worker.pid, worker.root, worker.socket) == ("abc123", 7, "/r", "/s.sock")

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0s"), (5, "5s"), (65, "1m5s"), (3661, "1h1m")],
        ids=["zero", "secs", "mins", "hours"],
    )
    def test_format_uptime(self, seconds: int, expected: str) -> None:
        assert ops.format_uptime(seconds) == expected
