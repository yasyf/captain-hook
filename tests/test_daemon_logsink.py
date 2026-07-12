from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from captain_hook.daemon.context import request_scope
from captain_hook.daemon.logsink import SessionFileRouter, configure_daemon_logging, daemon_log_path
from captain_hook.daemon.protocol import ClientInfo, Request

KEY = "deadbeefcafe0001"


def make_request(log_dir: Path) -> Request:
    return Request(
        v=1,
        kind="event",
        client=ClientInfo(version="", build="b", pid=1, ppid=2),
        event="PreToolUse",
        root="/tmp/proj",
        cwd="/tmp/proj",
        env={"CAPTAIN_HOOK_LOG_DIR": str(log_dir)},
        payload_raw="{}",
    )


@pytest.fixture(autouse=True)
def restore_loguru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(tmp_path / "run"))
    yield
    logger.remove()


class TestConfigureDaemonLogging:
    def test_session_file_format_matches_cold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        logs = tmp_path / "logs"
        configure_daemon_logging(KEY)
        with request_scope(make_request(logs), "sess-fmt"):
            logger.info("format check")
        daemon_line = (logs / "sess-fmt.log").read_text()

        logger.remove()
        from captain_hook.log import setup_logging

        cold_dir = tmp_path / "cold"
        monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(cold_dir))
        setup_logging("sess-fmt")
        logger.info("format check")
        logger.remove()
        cold_line = (cold_dir / "sess-fmt.log").read_text()

        assert daemon_line.endswith(" INFO tests.test_daemon_logsink: format check\n")
        assert daemon_line.split(" INFO ", 1)[1] == cold_line.split(" INFO ", 1)[1]

    def test_user_bound_context_renders_but_routing_keys_are_stripped(self, tmp_path: Path) -> None:
        logs = tmp_path / "logs"
        configure_daemon_logging(KEY)
        with request_scope(make_request(logs), "sess-ctx"):
            logger.bind(marker="keep").info("with context")
        line = (logs / "sess-ctx.log").read_text()
        assert line.endswith(" INFO tests.test_daemon_logsink: with context | {'marker': 'keep'}\n")
        assert "session_log_path" not in line
        assert "session_id" not in line

    def test_bound_record_stays_out_of_daemon_log(self, tmp_path: Path) -> None:
        logs = tmp_path / "logs"
        configure_daemon_logging(KEY)
        with request_scope(make_request(logs), "sess-only"):
            logger.info("session-scoped")
        logger.complete()
        daemon_log = daemon_log_path(KEY)
        assert "session-scoped" in (logs / "sess-only.log").read_text()
        assert not daemon_log.exists() or "session-scoped" not in daemon_log.read_text()

    def test_long_bound_value_truncates_but_routing_path_survives(self, tmp_path: Path) -> None:
        logs = tmp_path / "deeply" / "nested" / ("p" * 200) / "logs"
        configure_daemon_logging(KEY)
        with request_scope(make_request(logs), "sess-long"):
            logger.bind(blob="A" * 1000).info("long")
        # The router wrote to the un-truncated session path even though it exceeds the 200-char cap.
        line = (logs / "sess-long.log").read_text()
        assert "A" * 200 + "…" in line
        assert "A" * 201 not in line

    def test_routes_records_to_per_session_files(self, tmp_path: Path) -> None:
        logs = tmp_path / "logs"
        configure_daemon_logging(KEY)
        with request_scope(make_request(logs), "sess-a"):
            logger.info("for a")
        with request_scope(make_request(logs), "sess-b"):
            logger.info("for b")
        assert "for a" in (logs / "sess-a.log").read_text()
        assert "for b" in (logs / "sess-b.log").read_text()
        assert "for b" not in (logs / "sess-a.log").read_text()

    def test_daemon_log_catches_unbound_records(self, tmp_path: Path) -> None:
        logs = tmp_path / "logs"
        configure_daemon_logging(KEY)
        logger.info("boot message")
        logger.complete()
        assert "boot message" in daemon_log_path(KEY).read_text()
        assert not (logs / "unknown.log").exists()
        assert daemon_log_path(KEY) == logs / f"daemon-{KEY}.log"

    def test_stderr_tee_mirrors_warning_into_request_buffer(self, tmp_path: Path) -> None:
        configure_daemon_logging(KEY)
        with request_scope(make_request(tmp_path / "logs"), "sess-tee") as buffers:
            logger.warning("danger")
            logger.info("quiet")
        assert buffers.stderr.getvalue() == "WARNING: danger\n"

    def test_stderr_tee_silent_without_a_request(self, tmp_path: Path) -> None:
        configure_daemon_logging(KEY)
        # No request bound: a WARNING goes to the daemon log, teeing into no buffer (no crash).
        logger.warning("orphan warning")
        logger.complete()
        assert "orphan warning" in daemon_log_path(KEY).read_text()


class TestSessionFileRouter:
    def test_lru_evicts_and_closes_least_recent_handle(self, tmp_path: Path) -> None:
        router = SessionFileRouter(maxsize=2)
        handle_a = router._handle(str(tmp_path / "a.log"))
        router._handle(str(tmp_path / "b.log"))
        assert not handle_a.closed
        router._handle(str(tmp_path / "c.log"))
        assert handle_a.closed
        assert len(router._handles) == 2
        router.close()

    def test_reused_path_keeps_one_handle(self, tmp_path: Path) -> None:
        router = SessionFileRouter(maxsize=2)
        first = router._handle(str(tmp_path / "x.log"))
        again = router._handle(str(tmp_path / "x.log"))
        assert first is again
        router.close()
        assert first.closed
