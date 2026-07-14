from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cc_transcript.ids import SessionId
from click.testing import CliRunner

from captain_hook.cli import cli
from captain_hook.packs import bootstrap, manager
from captain_hook.session import ensure_session
from tests.helpers import run_cli

# --- fixtures / helpers --------------------------------------------------------------


def config_dir(tmp_path: Path, *, known: bool) -> Path:
    """A CLAUDE_CONFIG_DIR whose known_marketplaces.json does or doesn't list captain-hook."""
    config = tmp_path / "config"
    (config / "plugins").mkdir(parents=True)
    payload = {"captain-hook": {}} if known else {"other-marketplace": {}}
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps(payload))
    return config


def use_config(monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))


def claude_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/claude")


def claude_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)


def forbid_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"unexpected Popen: {args!r}")

    monkeypatch.setattr(bootstrap.subprocess, "Popen", boom)


def capture_popen(monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
    calls: list[SimpleNamespace] = []

    def fake(argv: list[str], **kwargs: object) -> object:
        calls.append(SimpleNamespace(argv=argv, kwargs=kwargs))
        return MagicMock()

    monkeypatch.setattr(bootstrap.subprocess, "Popen", fake)
    return calls


def capture_run(monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
    calls: list[SimpleNamespace] = []

    def fake(argv: list[str], **kwargs: object) -> object:
        calls.append(SimpleNamespace(argv=argv, kwargs=kwargs))
        return MagicMock(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    return calls


def make_pack(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / manager.PACK_MANIFEST).write_text(f'name = "{name}"\nversion = "0.1.0"\ndescription = "d"\nhooks = "."\n')
    (root / "h.py").write_text('from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message="m")\n')
    return root


# --- marketplace_known predicate -----------------------------------------------------


def test_known_marketplaces_with_captain_hook_key_is_known(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=True))
    assert bootstrap.marketplace_known() is True


def test_missing_known_marketplaces_counts_as_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, tmp_path / "no-such-config")
    assert bootstrap.marketplace_known() is False


def test_malformed_known_marketplaces_counts_as_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = config_dir(tmp_path, known=False)
    (config / "plugins" / "known_marketplaces.json").write_text("{ not json")
    use_config(monkeypatch, config)
    assert bootstrap.marketplace_known() is False


def test_non_object_known_marketplaces_counts_as_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = config_dir(tmp_path, known=False)
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps(["captain-hook"]))
    use_config(monkeypatch, config)
    assert bootstrap.marketplace_known() is False


# --- maybe_bootstrap hot path --------------------------------------------------------


def test_marketplace_known_is_hard_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=True))
    forbid_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap() is None
    assert not bootstrap.marker_path().exists()  # zero further I/O — no attempt marker written


def test_spawn_uses_exact_detached_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_present(monkeypatch)
    calls = capture_popen(monkeypatch)

    assert bootstrap.maybe_bootstrap() == bootstrap.BOOTSTRAP_NOTICE
    assert len(calls) == 1
    call = calls[0]
    # -I isolates the detached worker from a cwd/PYTHONPATH captain_hook shadow (venv site-packages
    # still resolve, so `-I -m captain_hook` runs the installed package).
    assert call.argv == [sys.executable, "-I", "-m", "captain_hook", "pack", "bootstrap"]
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["start_new_session"] is True
    # all three fds redirected; stdout and stderr share the one log handle so nothing inherits a pipe
    assert call.kwargs["stdout"] is not None
    assert call.kwargs["stdout"] is call.kwargs["stderr"]
    assert bootstrap.marker_path().is_file()  # the attempt is recorded before the spawn


def test_fresh_marker_suppresses_respawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_present(monkeypatch)
    bootstrap.record_attempt(bootstrap.marker_path(), time.time())
    forbid_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap() is None  # within the hourly cooldown → no re-spawn


def test_expired_marker_allows_respawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_present(monkeypatch)
    bootstrap.record_attempt(bootstrap.marker_path(), time.time() - bootstrap.RETRY_COOLDOWN_SECONDS - 1)
    calls = capture_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap() == bootstrap.BOOTSTRAP_NOTICE
    assert len(calls) == 1  # a stale marker re-arms the spawn


def test_missing_claude_binary_records_attempt_and_skips_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_absent(monkeypatch)
    forbid_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap() is None
    assert bootstrap.marker_path().is_file()  # attempt recorded so it enters the cooldown, no busy re-check


# --- run_bootstrap worker ------------------------------------------------------------


def test_run_bootstrap_adds_marketplace_then_installs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_present(monkeypatch)  # which("claude") → the absolute path both calls must carry
    calls = capture_run(monkeypatch)
    bootstrap.run_bootstrap()
    assert [c.argv for c in calls] == [
        ["/usr/bin/claude", "plugin", "marketplace", "add", "yasyf/captain-hook"],
        ["/usr/bin/claude", "plugin", "install", "captain-hook@captain-hook"],
    ]
    assert all(c.kwargs["check"] is True and c.kwargs["timeout"] == 300 for c in calls)


def test_run_bootstrap_missing_claude_exits_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No claude on PATH: the worker resolves an absolute path first, logs, and exits without ever
    # invoking the literal "claude" (pre-fix it ran subprocess.run(["claude", ...])).
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_absent(monkeypatch)
    calls = capture_run(monkeypatch)
    bootstrap.run_bootstrap()
    assert calls == []


def test_run_bootstrap_short_circuits_when_sibling_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=True))
    calls = capture_run(monkeypatch)
    bootstrap.run_bootstrap()
    assert calls == []  # a sibling session registered the marketplace under the lock → no claude calls


def test_maybe_bootstrap_rechecks_known_under_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A sibling registers the marketplace between the lock-free outer check and lock acquisition;
    # the under-lock re-check catches it, so no second worker spawns (pre-fix: no re-check → spawn).
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    claude_present(monkeypatch)
    forbid_popen(monkeypatch)
    seen = iter([False, True])
    monkeypatch.setattr(bootstrap, "marketplace_known", lambda: next(seen))
    assert bootstrap.maybe_bootstrap() is None


def test_marker_with_non_numeric_attempted_at_is_not_fresh(tmp_path: Path) -> None:
    # A hand-corrupted marker whose attempted_at isn't a number re-arms the attempt rather than
    # raising TypeError on the `now - attempted_at` subtraction.
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"attempted_at": "not-a-number"}))
    assert bootstrap.attempt_fresh(marker, time.time()) is False


# --- attach wiring -------------------------------------------------------------------


def test_attach_survives_bootstrap_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, config_dir(tmp_path, known=False))
    pack = make_pack(tmp_path / "ccx", "ccx")

    def boom() -> str | None:
        raise RuntimeError("bootstrap blew up")

    monkeypatch.setattr(bootstrap, "maybe_bootstrap", boom)
    result = CliRunner().invoke(cli, ["pack", "attach", str(pack)], input=json.dumps({"session_id": "sess-x"}))

    assert result.exit_code == 0, result.output  # fail-soft: the bootstrap error never breaks the attach
    assert bootstrap.BOOTSTRAP_NOTICE not in result.output
    recorded = manager.read_attached(ensure_session(SessionId("sess-x")))
    assert [p.name for p in recorded] == ["ccx"]  # the pack still landed


# --- end-to-end with a stub claude on PATH -------------------------------------------


def test_stub_claude_e2e_records_argv_and_registers_marketplace(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "plugins").mkdir(parents=True)
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps({"other-marketplace": {}}))
    known = config / "plugins" / "known_marketplaces.json"

    argv_log = tmp_path / "claude-argv.log"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$ARGV_LOG"\n'
        'if [ "$1" = "plugin" ] && [ "$2" = "marketplace" ] && [ "$3" = "add" ]; then\n'
        '  mkdir -p "$CLAUDE_CONFIG_DIR/plugins"\n'
        '  printf \'{"captain-hook": {}}\' > "$CLAUDE_CONFIG_DIR/plugins/known_marketplaces.json"\n'
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    pack = make_pack(tmp_path / "ccx", "ccx")
    env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_CONFIG_DIR": str(config),
        "ARGV_LOG": str(argv_log),
    }

    first = run_cli("pack", "attach", str(pack), stdin_data=json.dumps({"session_id": "sess-e2e"}), env=env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == bootstrap.BOOTSTRAP_NOTICE  # the one deliberate stdout

    def logged() -> str:
        return argv_log.read_text() if argv_log.exists() else ""

    deadline = time.time() + 30
    while time.time() < deadline and "plugin install" not in logged():
        time.sleep(0.1)

    assert "plugin marketplace add yasyf/captain-hook" in logged()
    assert "plugin install captain-hook@captain-hook" in logged()
    assert "captain-hook" in known.read_text()  # the worker registered the marketplace

    second = run_cli("pack", "attach", str(pack), stdin_data=json.dumps({"session_id": "sess-e2e-2"}), env=env)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""  # marketplace now known → hard no-op, no notice
