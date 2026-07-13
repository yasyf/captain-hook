from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from capt_hook_client import client, key

BLOCK_HOOK = (
    "from captain_hook import hook, Event\nhook(Event.PreToolUse, message='blocked by test hook', block=True)\n"
)
# A PreToolUse hook whose only effect is appending to a sink file named by CAPT_HOOK_MARKER_SINK.
# The fake daemons never dispatch it, so a written sink means the client ran a COLD fallback — the
# probe for single-dispatch in the post-send fail-open cases.
MARKER_HOOK = (
    "import os\n"
    "from pathlib import Path\n"
    "from captain_hook import Event, on\n"
    "\n"
    "\n"
    "@on(Event.PreToolUse)\n"
    "def mark(evt):\n"
    "    if sink := os.environ.get('CAPT_HOOK_MARKER_SINK'):\n"
    "        Path(sink).open('a').write('fired\\n')\n"
    "    return None\n"
)
PAYLOAD = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})


@pytest.fixture
def run_dir() -> Path:
    # Short /tmp dir: the daemon socket path must stay under the macOS 104-byte sun_path cap.
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="chc-"))
    assert len(str(path)) < 70
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hooks"
    d.mkdir()
    (d / "__init__.py").write_text("")
    (d / "conf.py").write_text(BLOCK_HOOK)
    return d


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    # Hooks at the default <root>/.claude/hooks discovery location: the daemon and the cold CLI
    # both find them with no --hooks, so the same project drives the warm and the cold path.
    d = tmp_path / ".claude" / "hooks"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "conf.py").write_text(BLOCK_HOOK)
    return tmp_path


@pytest.fixture
def marker_dir(tmp_path: Path) -> Path:
    # A project whose sole hook records that a cold run happened; used to prove the client did NOT
    # rerun cold on the post-send fail-open and status=error paths.
    d = tmp_path / ".claude" / "hooks"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "conf.py").write_text(MARKER_HOOK)
    return tmp_path


def client_env(run_dir: Path, **overrides: str) -> dict[str, str]:
    return {**os.environ, "CAPT_HOOK_RUN_DIR": str(run_dir), "CAPT_HOOK_ONCE_TTL": "0", **overrides}


def run_client(
    *args: str, env: dict[str, str], stdin: str = "", cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "capt_hook_client", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def run_cold(
    *args: str, env: dict[str, str], stdin: str = "", cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "captain_hook", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


class FakeDaemon:
    def __init__(self, sock_path: str, response: dict[str, object] | None, *, close_after_read: bool = False) -> None:
        self.response = response
        self.close_after_read = close_after_read
        self.requests: list[dict[str, object]] = []
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                if not (line := read_line(conn)):
                    continue  # peer closed before sending (e.g. refused on the client's peer-uid check)
                self.requests.append(json.loads(line))
                if self.close_after_read:
                    continue  # read the request, then drop without responding — a deterministic post-send EOF
                if self.response is None:
                    self._stop.wait(10)
                else:
                    conn.sendall((json.dumps(self.response) + "\n").encode())

    def close(self) -> None:
        self._stop.set()
        self._srv.close()
        self._thread.join(timeout=2)


def read_line(conn: socket.socket) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


class ThrottledServer:
    """A listener that accepts and never reads, with a tiny receive buffer: a client sending more
    than the socket buffers hold blocks and — under a short timeout — its ``sendall`` raises before
    delivering a full request line, the incomplete-send (pre-send) case R1 must classify as cold."""

    def __init__(self, sock_path: str) -> None:
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        self._srv.bind(sock_path)
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._conns.append(self._srv.accept()[0])  # accept, then never read
            except TimeoutError:
                continue
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        self._srv.close()
        for conn in self._conns:
            conn.close()
        self._thread.join(timeout=2)


def preseed_throttled(run_dir: Path, root: str, env: dict[str, str]) -> ThrottledServer:
    sock_path = str(run_dir / f"{key.worker_key(root, env)}.sock")
    assert len(sock_path) < 100
    return ThrottledServer(sock_path)


def preseed_daemon(
    run_dir: Path,
    root: str,
    env: dict[str, str],
    response: dict[str, object] | None,
    *,
    close_after_read: bool = False,
) -> FakeDaemon:
    worker = key.worker_key(root, env)
    sock_path = str(run_dir / f"{worker}.sock")
    assert len(sock_path) < 100
    return FakeDaemon(sock_path, response, close_after_read=close_after_read)


class TestParseArgv:
    @pytest.mark.parametrize(
        "argv, expected",
        [
            (
                ["run", "PreToolUse"],
                {"verb": "run", "event": "PreToolUse", "async": False, "root": None, "hooks": None},
            ),
            (
                ["run", "PreToolUse", "--async"],
                {"verb": "run", "event": "PreToolUse", "async": True, "root": None, "hooks": None},
            ),
            (["ping"], {"verb": "ping", "root": None, "hooks": None}),
            (
                ["--root", "/p", "--hooks", "/h", "run", "Stop"],
                {"verb": "run", "event": "Stop", "async": False, "root": "/p", "hooks": "/h"},
            ),
            (
                ["--root=/p", "--hooks=/h", "ping"],
                {"verb": "ping", "root": "/p", "hooks": "/h"},
            ),
        ],
    )
    def test_recognized(self, argv: list[str], expected: dict[str, object]) -> None:
        assert client.parse_argv(argv) == expected

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["review", "run"],
            ["run"],
            ["run", "--async", "PreToolUse"],
            ["run", "-PreToolUse"],
            ["ping", "extra"],
            ["--help"],
            ["--root"],
            ["status", "--json"],
        ],
    )
    def test_unrecognized_passthrough(self, argv: list[str]) -> None:
        assert client.parse_argv(argv) is None


class TestDeadline:
    def test_default_is_30s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_CLIENT_TIMEOUT", raising=False)
        assert client.deadline_seconds("PreToolUse") == 30.0

    def test_user_prompt_submit_undercuts_to_20s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_CLIENT_TIMEOUT", raising=False)
        assert client.deadline_seconds("UserPromptSubmit") == 20.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_CLIENT_TIMEOUT", "2.5")
        assert client.deadline_seconds("UserPromptSubmit") == 2.5


class TestBuildRequest:
    def test_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_CLIENT_BUILD", "b-1")
        monkeypatch.setenv("CAPT_HOOK_MARKER", "seen")
        request = client.build_request("PreToolUse", "/proj", PAYLOAD, async_=True)
        assert request["v"] == key.PROTOCOL
        assert request["kind"] == "event"
        assert request["event"] == "PreToolUse"
        assert request["async"] is True
        assert request["root"] == "/proj"
        assert request["hooks"] is None
        assert request["payload_raw"] == PAYLOAD
        assert request["client"] == {"version": "", "build": "b-1", "pid": os.getpid(), "ppid": os.getppid()}
        assert request["env"]["CAPT_HOOK_MARKER"] == "seen"
        assert "PATH" not in request["env"]


class TestFallbackMatrix:
    def test_no_daemon_env_is_straight_cold(self, run_dir: Path, project_dir: Path) -> None:
        env = client_env(run_dir, CAPT_HOOK_NO_DAEMON="1")
        result = run_client("--root", str(project_dir), "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert result.returncode == 0
        assert "permissionDecision" in result.stdout
        # Straight cold means no socket handshake was ever attempted.
        assert not list(run_dir.glob("*.sock")) and not list(run_dir.glob("*.lock"))

    def test_spawn_early_exit_falls_back_cold_fast(self, run_dir: Path, project_dir: Path) -> None:
        # Over-long run dir → socket path past the sun_path cap → the spawned worker aborts at
        # bind; the client must detect the early exit and run cold fast.
        env = client_env(run_dir, CAPT_HOOK_RUN_DIR=str(run_dir / ("d" * 90)))
        start = time.monotonic()
        result = run_client("--root", str(project_dir), "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert time.monotonic() - start < 10.0
        assert result.returncode == 0
        assert "permissionDecision" in result.stdout
        assert "hook: worker exited early" in result.stderr

    def test_deadline_expiry_fails_open_never_cold(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_CLIENT_TIMEOUT="0.4", CAPT_HOOK_DAEMON_FALLBACK="closed")
        daemon = preseed_daemon(run_dir, root, env, response=None)
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert len(daemon.requests) == 1


class TestRoundTrip:
    def _env(self, run_dir: Path) -> dict[str, str]:
        return client_env(run_dir, CAPT_HOOK_DAEMON_FALLBACK="closed")

    def test_stdout_written_verbatim_without_added_newline(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "NO-NEWLINE", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.stdout == "NO-NEWLINE"
        assert result.returncode == 0

    def test_stdout_preserves_daemon_trailing_newline(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "WITH-NL\n", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.stdout == "WITH-NL\n"

    def test_exit_code_propagated(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "", "exit": 2})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 2

    def test_request_shape_reaches_daemon(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "", "exit": 0})
        try:
            run_client("--root", root, "run", "PreToolUse", "--async", env=env, stdin=PAYLOAD, cwd=root)
        finally:
            daemon.close()
        assert len(daemon.requests) == 1
        request = daemon.requests[0]
        assert request["v"] == 1
        assert request["kind"] == "event"
        assert request["event"] == "PreToolUse"
        assert request["async"] is True
        assert request["root"] == root
        assert request["hooks"] is None
        assert request["payload_raw"] == PAYLOAD
        assert os.path.realpath(str(request["cwd"])) == os.path.realpath(root)

    def test_protocol_mismatch_response_fails_open(self, run_dir: Path, hooks_dir: Path) -> None:
        # A v-mismatched response arrives AFTER the request was sent, so the worker may have
        # dispatched: the client fails open (exit 0, no output) rather than cold-rerunning.
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 99, "status": "ok", "stdout": "x", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0
        assert result.stdout == ""


class TestExchangeBoundary:
    """The classification boundary R1 fixes, unit-level and deterministic: a ``sendall`` that raises
    delivered an incomplete request line (pre-send → cold); a failure while reading the response is
    post-send (fail open)."""

    class SendFails:
        def settimeout(self, _: float) -> None: ...

        def sendall(self, _: bytes) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        def recv(self, _: int) -> bytes:
            raise AssertionError("must not read the response after an incomplete send")

    class ReadEOF:
        def settimeout(self, _: float) -> None: ...

        def sendall(self, _: bytes) -> None: ...

        def recv(self, _: int) -> bytes:
            return b""

    def test_incomplete_send_is_presend_unavailable(self) -> None:
        with pytest.raises(client.DaemonUnavailable):
            client.exchange(self.SendFails(), {"kind": "ping"}, time.monotonic() + 5)

    def test_read_failure_after_send_is_postsend(self) -> None:
        with pytest.raises(client.PostSendFailure):
            client.exchange(self.ReadEOF(), {"kind": "ping"}, time.monotonic() + 5)


class TestSendBoundary:
    """Whether the request was sent decides the fallback: pre-send → cold, post-send → fail open."""

    def _cold_env(self, run_dir: Path, marker_dir: Path, **overrides: str) -> tuple[dict[str, str], Path]:
        sink = marker_dir / "cold-sink.txt"
        return client_env(run_dir, CAPT_HOOK_MARKER_SINK=str(sink), **overrides), sink

    def test_incomplete_send_falls_back_cold(self, run_dir: Path, project_dir: Path) -> None:
        # A worker that accepts but never drains its receive buffer: a large request blocks in
        # sendall and, under a short deadline, raises before the full line lands. The worker never
        # dispatched, so this pre-send failure runs cold and delivers the gate hook's deny envelope.
        root = str(project_dir)
        env = client_env(run_dir, CAPT_HOOK_CLIENT_TIMEOUT="1.0")  # default fallback: cold
        big_payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "x" * (512 * 1024)}})
        server = preseed_throttled(run_dir, root, env)
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=big_payload)
        finally:
            server.close()
        assert result.returncode == 0, result.stderr
        assert "permissionDecision" in result.stdout, "the pre-send failure did not run cold"

    def test_response_missing_exit_fails_open_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # A status=ok response with no exit field is malformed → post-send fail open (exit 0, no
        # output), never a silent exit 0 relaying the daemon's stdout, and never a cold rerun.
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "SHOULD_NOT_APPEAR"})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", "a malformed response must not relay stdout"
        assert not sink.exists(), "a malformed response must not trigger a cold rerun"

    def test_response_string_exit_fails_open_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # A non-integer exit ("1") is malformed → post-send fail open, no stdout relay, no cold rerun.
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "X", "exit": "1"})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert not sink.exists(), "a malformed response must not trigger a cold rerun"

    def test_response_unknown_status_fails_open_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # An unknown status is not a provable non-dispatch (unlike "rejected"): the worker may have
        # dispatched, so it is malformed → post-send fail open, never a cold rerun (double-dispatch).
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "weird", "stdout": "", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert not sink.exists(), "an unknown status must not trigger a cold rerun"

    def test_post_send_eof_fails_open_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # The daemon reads the request (so the client definitively sent) then drops without responding;
        # the read hits EOF. That is post-send, so it fails open and never reruns cold.
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        daemon = preseed_daemon(run_dir, root, env, response=None, close_after_read=True)
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert len(daemon.requests) == 1, "the request was not delivered before the EOF (not a post-send case)"
        assert not sink.exists(), "a cold rerun fired the marker hook — double dispatch"

    def test_post_send_stall_fails_open_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # The daemon reads the request then stalls past the deadline; a post-send deadline fails open.
        root = str(marker_dir)
        env, sink = self._cold_env(
            run_dir, marker_dir, CAPT_HOOK_CLIENT_TIMEOUT="0.4", CAPT_HOOK_DAEMON_FALLBACK="cold"
        )
        daemon = preseed_daemon(run_dir, root, env, response=None)
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert len(daemon.requests) == 1
        assert not sink.exists(), "a cold rerun fired the marker hook — double dispatch"

    def test_error_status_relayed_verbatim_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # status=error means the worker dispatched and hit an uncaught error: relay its stderr and exit
        # verbatim, never rerun cold (which the consumed once-sentinel would silently swallow).
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        response = {"v": 1, "status": "error", "stdout": "", "stderr": "Traceback: boom\n", "exit": 1}
        daemon = preseed_daemon(run_dir, root, env, response)
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 1
        assert result.stderr == "Traceback: boom\n"
        assert not sink.exists(), "status=error must not trigger a cold rerun"

    def test_response_bool_exit_fails_open_no_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # V12: a bool is an int, so exit:true is malformed → post-send fail open, no stdout relay, no cold.
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "X", "exit": True})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert not sink.exists(), "a bool exit must not trigger a cold rerun"

    def test_rejected_with_malformed_exit_fails_open_not_cold(self, run_dir: Path, marker_dir: Path) -> None:
        # V12: a malformed rejected (exit:true) is not a provable non-dispatch → fail open, never rerun cold.
        root = str(marker_dir)
        env, sink = self._cold_env(run_dir, marker_dir, CAPT_HOOK_DAEMON_FALLBACK="cold")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "rejected", "exit": True})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert not sink.exists(), "a malformed rejected must not trigger a cold rerun"

    def test_rejected_status_falls_back_cold(self, run_dir: Path, project_dir: Path) -> None:
        # status=rejected is a provable non-dispatch (protocol gate): cold fallback stays correct.
        root = str(project_dir)
        env = client_env(run_dir)  # default fallback: cold
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "rejected", "stdout": "", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 0
        assert "permissionDecision" in result.stdout  # the cold block hook produced its deny envelope

    def test_presend_deadline_falls_back_cold(self, run_dir: Path, project_dir: Path) -> None:
        # A held spawn flock with no socket keeps the client from ever reaching a worker; the pre-send
        # deadline must go cold (default fallback), delivering the gate hook's deny envelope.
        root = str(project_dir)
        env = client_env(run_dir, CAPT_HOOK_CLIENT_TIMEOUT="0.4")
        lock_path = run_dir / f"{key.worker_key(root, env)}.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        assert result.returncode == 0, result.stderr
        assert "permissionDecision" in result.stdout
        assert not list(run_dir.glob("*.sock")), "no worker should have bound a socket"

    def test_bogus_run_dir_falls_back_cold_matching_cold_cli(self, run_dir: Path, project_dir: Path) -> None:
        # os.makedirs on a non-directory run dir raises NotADirectoryError inside the spawn machinery;
        # that OSError is a pre-send failure and goes cold, byte-for-byte with the cold CLI.
        root = str(project_dir)
        env = client_env(run_dir, CAPT_HOOK_RUN_DIR="/dev/null/nope")
        via_client = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        via_cold = run_cold("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert via_client.returncode == via_cold.returncode
        assert via_client.stdout == via_cold.stdout
        assert "permissionDecision" in via_client.stdout

    def test_log_path_is_a_directory_falls_back_cold(self, run_dir: Path, project_dir: Path) -> None:
        # The worker log path pre-seeded as a directory makes the spawn's log open raise
        # IsADirectoryError — a pre-send OSError that goes cold.
        root = str(project_dir)
        env = client_env(run_dir)
        (run_dir / f"{key.worker_key(root, env)}.log").mkdir()
        result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert result.returncode == 0, result.stderr
        assert "permissionDecision" in result.stdout


class TestRunDirTrust:
    """S1/S2: the client refuses a pre-existing socket in a run dir it cannot trust, so an attacker
    who plants a socket in a world-writable or symlinked run dir cannot intercept the request."""

    def test_absent_run_dir_is_allowed(self, tmp_path: Path) -> None:
        client.ensure_trusted_run_dir(str(tmp_path / "does-not-exist"))  # spawn creates it 0700

    def test_private_run_dir_is_allowed(self, run_dir: Path) -> None:
        os.chmod(run_dir, 0o700)
        client.ensure_trusted_run_dir(str(run_dir))

    def test_world_writable_run_dir_is_refused(self, run_dir: Path) -> None:
        os.chmod(run_dir, 0o777)
        with pytest.raises(client.DaemonUnavailable):
            client.ensure_trusted_run_dir(str(run_dir))

    def test_symlinked_run_dir_is_refused(self, tmp_path: Path, run_dir: Path) -> None:
        link = tmp_path / "run-link"
        link.symlink_to(run_dir)
        with pytest.raises(client.DaemonUnavailable):
            client.ensure_trusted_run_dir(str(link))

    def test_world_writable_run_dir_runs_cold_not_the_planted_socket(self, run_dir: Path, project_dir: Path) -> None:
        os.chmod(run_dir, 0o777)
        root = str(project_dir)
        env = client_env(run_dir)  # default fallback: cold
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "FROM-DAEMON", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert daemon.requests == [], "the client trusted a socket in a world-writable run dir"
        assert "FROM-DAEMON" not in result.stdout
        assert "permissionDecision" in result.stdout  # the cold block hook fired instead
        assert result.returncode == 0

    def test_untrusted_run_dir_forces_cold_even_under_fallback_open(self, run_dir: Path, project_dir: Path) -> None:
        # V2: an untrusted run dir is a security precondition, not an availability condition — it must run
        # cold regardless of CAPT_HOOK_DAEMON_FALLBACK, so a gate deny is never lost to a fail-open exit 0.
        os.chmod(run_dir, 0o777)
        root = str(project_dir)
        env = client_env(run_dir, CAPT_HOOK_DAEMON_FALLBACK="open")
        result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert result.returncode == 0, result.stderr
        assert "permissionDecision" in result.stdout, "an untrusted run dir failed open instead of running cold"


class TestPeerUidVerification:
    """V1/V11: the authority closing the run-dir TOCTOU is the per-connection peer-uid check — a socket
    planted by another uid is refused after connect, before the request is ever sent."""

    def test_same_process_peer_reads_our_euid(self) -> None:
        left, right = socket.socketpair()
        try:
            assert client.peer_uid(left) == os.geteuid()
            client.verify_peer(left, "/x.sock")  # matching peer → no raise
        finally:
            left.close()
            right.close()

    def test_foreign_peer_uid_is_refused_and_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client, "peer_uid", lambda _s: os.geteuid() + 1)
        left, right = socket.socketpair()
        try:
            with pytest.raises(client.UntrustedWorker):
                client.verify_peer(left, "/x.sock")
            with pytest.raises(OSError):
                left.sendall(b"x")  # verify_peer closed our side on refusal
        finally:
            left.close()
            right.close()

    def test_event_peer_mismatch_runs_cold_never_sends(
        self, run_dir: Path, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A live stub socket at the worker path, but the peer-uid check is forced to mismatch: the client
        # refuses it and runs cold (even under FALLBACK=open), never sending the request to the socket.
        root = str(project_dir)
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run_dir))
        monkeypatch.setenv("CAPT_HOOK_ONCE_TTL", "0")
        monkeypatch.setenv("CAPT_HOOK_DAEMON_FALLBACK", "open")
        daemon = preseed_daemon(run_dir, root, dict(os.environ), {"v": 1, "status": "ok", "stdout": "SPOOF", "exit": 0})
        monkeypatch.setattr(client, "peer_uid", lambda _s: os.geteuid() + 1)
        cold_calls: list[tuple] = []
        monkeypatch.setattr(client, "cold", lambda args, payload: (cold_calls.append((args, payload)), 0)[1])
        monkeypatch.setattr(client.sys, "stdin", io.TextIOWrapper(io.BytesIO(PAYLOAD.encode())))
        try:
            rc = client.do_run("PreToolUse", root, ["run", "PreToolUse"], async_=False)
        finally:
            daemon.close()
        assert rc == 0
        assert cold_calls, "a peer-uid mismatch did not force the cold path"
        assert daemon.requests == [], "the client sent the request to an untrusted socket"

    def test_ping_peer_mismatch_exits_nonzero_without_printing(
        self, run_dir: Path, hooks_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = str(hooks_dir.parent)
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run_dir))
        daemon = preseed_daemon(run_dir, root, dict(os.environ), {"v": 1, "status": "ok", "exit": 0})
        monkeypatch.setattr(client, "peer_uid", lambda _s: os.geteuid() + 1)
        try:
            rc = client.do_ping(root)
        finally:
            daemon.close()
        assert rc == 1
        assert daemon.requests == [], "the client pinged an untrusted socket"
        assert capsys.readouterr().out == "", "a refused ping printed a spoofed response"


class TestPassthrough:
    def test_unknown_subcommand_execs_captain_hook(self, run_dir: Path) -> None:
        result = run_client("definitely-not-a-command", env=client_env(run_dir))
        assert result.returncode == 2
        assert "No such command" in result.stderr


class TestPing:
    def test_ping_no_daemon_exits_one(self, run_dir: Path) -> None:
        result = run_client("--root", "/tmp", "ping", env=client_env(run_dir))
        assert result.returncode == 1
        assert "hook:" in result.stderr
        # Connect-only: a ping with no daemon must never spawn one, so it leaves the run dir bare.
        assert not any(run_dir.iterdir())

    def test_ping_with_daemon_prints_response(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_DAEMON_FALLBACK="closed")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "exit": 0, "daemon": {"pid": 123}})
        try:
            result = run_client("--root", root, "ping", env=env)
        finally:
            daemon.close()
        assert result.returncode == 0
        assert json.loads(result.stdout)["status"] == "ok"
        assert daemon.requests[0]["kind"] == "ping"

    def test_ping_malformed_exit_exits_nonzero_without_printing(self, run_dir: Path, hooks_dir: Path) -> None:
        # V12: a ping response with a malformed exit (bool) is untrusted → the client exits nonzero and
        # prints nothing, never relaying the (untrusted) response to stdout.
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_DAEMON_FALLBACK="closed")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "exit": True})
        try:
            result = run_client("--root", root, "ping", env=env)
        finally:
            daemon.close()
        assert result.returncode == 1
        assert result.stdout == "", "a malformed ping response was printed as if trusted"


class TestColdParity:
    def test_client_cold_fallback_matches_cold_cli(self, run_dir: Path, project_dir: Path) -> None:
        root = str(project_dir)
        env = client_env(run_dir, CAPT_HOOK_NO_DAEMON="1")
        via_client = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        via_cold = run_cold("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert via_client.stdout == via_cold.stdout
        assert via_client.returncode == via_cold.returncode
        assert via_cold.stdout != ""

    def test_client_spawn_fallback_matches_cold_cli(self, run_dir: Path, project_dir: Path) -> None:
        # Over-long run dir → socket path past the sun_path cap → the spawned worker aborts at
        # bind; the client detects the early exit and runs cold, byte-for-byte with the cold CLI.
        root = str(project_dir)
        env = client_env(run_dir, CAPT_HOOK_RUN_DIR=str(run_dir / ("d" * 90)))
        via_client = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        via_cold = run_cold("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert "worker exited early" in via_client.stderr
        assert via_client.stdout == via_cold.stdout
        assert via_client.returncode == via_cold.returncode
        assert via_cold.stdout != ""

    def test_explicit_hooks_bypasses_a_live_daemon(self, run_dir: Path, hooks_dir: Path) -> None:
        # A custom --hooks request must run cold even with a warm worker listening: the daemon
        # only serves the root's own hooks, so passthrough is the only way to stay cold-identical.
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_DAEMON_FALLBACK="closed")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "stdout": "FROM-DAEMON", "exit": 0})
        try:
            result = run_client("--hooks", str(hooks_dir), "--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert daemon.requests == []
        assert "FROM-DAEMON" not in result.stdout
        assert "permissionDecision" in result.stdout
        assert result.returncode == 0


class TestImportPurity:
    def test_client_import_pulls_no_captain_hook_or_third_party(self) -> None:
        snippet = (
            "import sys\n"
            "before = set(sys.modules)\n"
            "import capt_hook_client.client\n"
            "new = set(sys.modules) - before\n"
            "stdlib = set(sys.stdlib_module_names)\n"
            "captain = sorted(m for m in new if m.startswith('captain_hook'))\n"
            "bad = sorted(m for m in new if m.split('.')[0] not in stdlib and not m.startswith('capt_hook_client'))\n"
            "assert not captain, captain\n"
            "assert not bad, bad\n"
        )
        result = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
