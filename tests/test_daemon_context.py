from __future__ import annotations

import io
import sys
import threading
from dataclasses import dataclass

from loguru import logger

from captain_hook.daemon import context
from captain_hook.daemon.context import ContextIO, install_context_io, request_scope
from captain_hook.util import reqenv


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ppid: int


@dataclass(frozen=True, slots=True)
class Request:
    client: ClientInfo
    cwd: str
    env: dict[str, str]


def make_request(*, env: dict[str, str] | None = None, cwd: str = "/tmp/proj") -> Request:
    return Request(
        client=ClientInfo(ppid=222),
        cwd=cwd,
        env=env or {},
    )


class TestRequestScope:
    def test_binds_env_cwd_and_client_identity(self) -> None:
        req = make_request(env={"HOOKS_PROFILE": "strict"}, cwd="/tmp/here")
        with request_scope(req, "sess-A"):
            ov = reqenv.current()
            assert ov is not None
            assert ov.session_id == "sess-A"
            assert ov.client_ppid == 222
            assert reqenv.getenv("HOOKS_PROFILE") == "strict"
            assert str(reqenv.cwd()) == "/tmp/here"
        assert reqenv.current() is None

    def test_absent_whitelisted_key_reads_as_unset(self) -> None:
        # A daemon-inherited var must not leak into a request that did not send it.
        with request_scope(make_request(env={}), "sess-B"):
            assert reqenv.getenv("HOOKS_PROFILE") is None

    def test_log_path_computed_inside_env_bind(self, tmp_path: object) -> None:
        req = make_request(env={"CAPTAIN_HOOK_LOG_DIR": "/req/logs"})
        seen: list[str] = []
        with request_scope(req, "sess-C"):
            sink_id = logger.add(lambda m: seen.append(m.record["extra"]["session_log_path"]), level="DEBUG")
            try:
                logger.info("x")
            finally:
                logger.remove(sink_id)
        assert seen == ["/req/logs/sess-C.log"]

    def test_missing_session_id_defaults_to_unknown(self) -> None:
        with request_scope(make_request(env={"CAPTAIN_HOOK_LOG_DIR": "/l"}), None) as buffers:
            assert reqenv.current().session_id == "unknown"
            assert buffers is not None


class TestContextIO:
    def test_bound_write_lands_in_request_buffer_not_fallback(self) -> None:
        fallback = io.StringIO()
        cio = ContextIO("stdout", fallback)
        with request_scope(make_request(), "s") as buffers:
            print("hello", file=cio)
        assert buffers.stdout.getvalue() == "hello\n"
        assert fallback.getvalue() == ""

    def test_unbound_write_lands_in_fallback(self) -> None:
        fallback = io.StringIO()
        cio = ContextIO("stderr", fallback)
        cio.write("stray\n")
        assert fallback.getvalue() == "stray\n"

    def test_thread_without_a_request_routes_to_fallback(self) -> None:
        fallback = io.StringIO()
        cio = ContextIO("stdout", fallback)
        with request_scope(make_request(), "s") as buffers:

            def worker() -> None:
                cio.write("from-thread\n")

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            cio.write("from-main\n")
        assert buffers.stdout.getvalue() == "from-main\n"
        assert fallback.getvalue() == "from-thread\n"

    def test_stderr_stream_routes_to_stderr_buffer(self) -> None:
        cio_out = ContextIO("stdout", io.StringIO())
        cio_err = ContextIO("stderr", io.StringIO())
        with request_scope(make_request(), "s") as buffers:
            cio_out.write("out")
            cio_err.write("err")
        assert buffers.stdout.getvalue() == "out"
        assert buffers.stderr.getvalue() == "err"


class TestInstallContextIO:
    def test_installs_once_and_captures_original_streams(self) -> None:
        orig_out, orig_err = sys.stdout, sys.stderr
        try:
            install_context_io()
            assert isinstance(sys.stdout, ContextIO)
            assert isinstance(sys.stderr, ContextIO)
            assert sys.stdout._fallback is orig_out
            installed = sys.stdout
            install_context_io()
            assert sys.stdout is installed
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err
        assert context.bound_buffers() is None
