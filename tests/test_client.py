from __future__ import annotations

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
    def __init__(self, sock_path: str, response: dict[str, object] | None) -> None:
        self.response = response
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
                self.requests.append(json.loads(read_line(conn)))
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


def preseed_daemon(run_dir: Path, root: str, env: dict[str, str], response: dict[str, object] | None) -> FakeDaemon:
    worker = key.worker_key(root, env)
    sock_path = str(run_dir / f"{worker}.sock")
    assert len(sock_path) < 100
    return FakeDaemon(sock_path, response)


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
        request = client.build_request("PreToolUse", "/proj", "/proj/.claude/hooks", PAYLOAD, async_=True)
        assert request["v"] == key.PROTOCOL
        assert request["kind"] == "event"
        assert request["event"] == "PreToolUse"
        assert request["async"] is True
        assert request["root"] == "/proj"
        assert request["hooks"] == "/proj/.claude/hooks"
        assert request["payload_raw"] == PAYLOAD
        assert request["client"] == {"version": "", "build": "b-1", "pid": os.getpid(), "ppid": os.getppid()}
        assert request["env"]["CAPT_HOOK_MARKER"] == "seen"
        assert "PATH" not in request["env"]


class TestFallbackMatrix:
    def test_no_daemon_env_is_straight_cold(self, run_dir: Path, hooks_dir: Path) -> None:
        env = client_env(run_dir, CAPT_HOOK_NO_DAEMON="1")
        result = run_client(
            "--hooks", str(hooks_dir), "--root", str(hooks_dir.parent), "run", "PreToolUse", env=env, stdin=PAYLOAD
        )
        assert result.returncode == 0
        assert "permissionDecision" in result.stdout
        # Straight cold means no socket handshake was ever attempted.
        assert not list(run_dir.glob("*.sock")) and not list(run_dir.glob("*.lock"))

    def test_spawn_early_exit_falls_back_cold_fast(self, run_dir: Path, hooks_dir: Path) -> None:
        env = client_env(run_dir)
        start = time.monotonic()
        result = run_client(
            "--hooks", str(hooks_dir), "--root", str(hooks_dir.parent), "run", "PreToolUse", env=env, stdin=PAYLOAD
        )
        assert time.monotonic() - start < 10.0
        assert result.returncode == 0
        assert "permissionDecision" in result.stdout
        assert "capt-hook-client: worker exited early" in result.stderr

    def test_deadline_expiry_fails_open_never_cold(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_CLIENT_TIMEOUT="0.4", CAPT_HOOK_DAEMON_FALLBACK="closed")
        daemon = preseed_daemon(run_dir, root, env, response=None)
        try:
            result = run_client("--hooks", str(hooks_dir), "--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
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
            run_client(
                "--hooks",
                str(hooks_dir),
                "--root",
                root,
                "run",
                "PreToolUse",
                "--async",
                env=env,
                stdin=PAYLOAD,
                cwd=root,
            )
        finally:
            daemon.close()
        assert len(daemon.requests) == 1
        request = daemon.requests[0]
        assert request["v"] == 1
        assert request["kind"] == "event"
        assert request["event"] == "PreToolUse"
        assert request["async"] is True
        assert request["root"] == root
        assert request["hooks"] == str(hooks_dir)
        assert request["payload_raw"] == PAYLOAD
        assert os.path.realpath(str(request["cwd"])) == os.path.realpath(root)

    def test_protocol_mismatch_fails_closed(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 99, "status": "ok", "stdout": "x", "exit": 0})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 1
        assert result.stdout == ""

    def test_daemon_error_status_fails_closed(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = self._env(run_dir)
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "error", "stdout": "", "exit": 1})
        try:
            result = run_client("--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        finally:
            daemon.close()
        assert result.returncode == 1


class TestPassthrough:
    def test_unknown_subcommand_execs_captain_hook(self, run_dir: Path) -> None:
        result = run_client("definitely-not-a-command", env=client_env(run_dir))
        assert result.returncode == 2
        assert "No such command" in result.stderr


class TestPing:
    def test_ping_no_daemon_exits_one(self, run_dir: Path) -> None:
        result = run_client("--root", "/tmp", "ping", env=client_env(run_dir))
        assert result.returncode == 1
        assert "worker exited early" in result.stderr

    def test_ping_with_daemon_prints_response(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_DAEMON_FALLBACK="closed")
        daemon = preseed_daemon(run_dir, root, env, {"v": 1, "status": "ok", "daemon": {"pid": 123}})
        try:
            result = run_client("--root", root, "ping", env=env)
        finally:
            daemon.close()
        assert result.returncode == 0
        assert json.loads(result.stdout)["status"] == "ok"
        assert daemon.requests[0]["kind"] == "ping"


class TestColdParity:
    def test_client_cold_fallback_matches_cold_cli(self, run_dir: Path, hooks_dir: Path) -> None:
        root = str(hooks_dir.parent)
        env = client_env(run_dir, CAPT_HOOK_NO_DAEMON="1")
        via_client = run_client("--hooks", str(hooks_dir), "--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        via_cold = run_cold("--hooks", str(hooks_dir), "--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert via_client.stdout == via_cold.stdout
        assert via_client.returncode == via_cold.returncode
        assert via_cold.stdout != ""

    def test_client_spawn_fallback_matches_cold_cli(self, run_dir: Path, hooks_dir: Path) -> None:
        # The real shipped C1 path: no daemon exists, so `daemon run` spawns and exits
        # immediately, the client detects the early exit and runs cold. stdout/exit must
        # match the cold CLI byte-for-byte (the breadcrumb lives on stderr only).
        root = str(hooks_dir.parent)
        env = client_env(run_dir)
        via_client = run_client("--hooks", str(hooks_dir), "--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        via_cold = run_cold("--hooks", str(hooks_dir), "--root", root, "run", "PreToolUse", env=env, stdin=PAYLOAD)
        assert "worker exited early" in via_client.stderr
        assert via_client.stdout == via_cold.stdout
        assert via_client.returncode == via_cold.returncode
        assert via_cold.stdout != ""


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
