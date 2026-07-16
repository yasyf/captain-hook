from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
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

CAPTAIN = "yasyf/captain-hook"
PRESENT = "yasyf/cc-present"

# --- fixtures / helpers --------------------------------------------------------------


def seed_config(tmp_path: Path, *repos: str) -> Path:
    """A CLAUDE_CONFIG_DIR whose known_marketplaces.json lists each ``owner/repo`` as a github source."""
    config = tmp_path / "config"
    (config / "plugins").mkdir(parents=True)
    payload = {repo: {"source": {"source": "github", "repo": repo}} for repo in repos}
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


def make_pack(root: Path, name: str, *, marketplaces: Sequence[str] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    mkt = f"marketplaces = {json.dumps(list(marketplaces))}\n" if marketplaces else ""
    (root / manager.PACK_MANIFEST).write_text(
        f'name = "{name}"\nversion = "0.1.0"\ndescription = "d"\nhooks = "."\n{mkt}'
    )
    (root / "h.py").write_text('from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message="m")\n')
    return root


def worker_repos(call: SimpleNamespace) -> list[str]:
    """The dependency-marketplace repos threaded onto a detached worker's argv (after `pack bootstrap`)."""
    return call.argv[call.argv.index("bootstrap") + 1 :]


# --- known_repos predicate -----------------------------------------------------------


def test_known_repos_collects_github_source_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path, CAPTAIN, PRESENT))
    assert bootstrap.known_repos() == {CAPTAIN, PRESENT}


def test_missing_known_marketplaces_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, tmp_path / "no-such-config")
    assert bootstrap.known_repos() == set()


def test_malformed_known_marketplaces_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = seed_config(tmp_path)
    (config / "plugins" / "known_marketplaces.json").write_text("{ not json")
    use_config(monkeypatch, config)
    assert bootstrap.known_repos() == set()


def test_non_object_known_marketplaces_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = seed_config(tmp_path)
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps([CAPTAIN]))
    use_config(monkeypatch, config)
    assert bootstrap.known_repos() == set()


def test_entry_without_github_source_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = seed_config(tmp_path)
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps({"captain-hook": {}}))
    use_config(monkeypatch, config)
    assert bootstrap.known_repos() == set()  # a legacy source-less entry no longer counts as known


# --- maybe_bootstrap hot path (case a) -----------------------------------------------


def test_all_required_known_is_hard_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path, CAPTAIN, PRESENT))
    forbid_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap([PRESENT]) is None
    # zero further I/O — no per-repo attempt marker written
    assert not bootstrap.marker_path(CAPTAIN).exists()
    assert not bootstrap.marker_path(PRESENT).exists()


# --- maybe_bootstrap miss path (case b) ----------------------------------------------


def test_two_unknown_repos_spawn_exact_argv_and_mark_each(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known
    claude_present(monkeypatch)
    calls = capture_popen(monkeypatch)

    notice = bootstrap.maybe_bootstrap([PRESENT])

    assert len(calls) == 1
    call = calls[0]
    # captain-hook (implicit, first) then the manifest's declared extra, in order, on one worker.
    assert call.argv == [sys.executable, "-I", "-m", "captain_hook", "pack", "bootstrap", CAPTAIN, PRESENT]
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["stdout"] is not None
    assert call.kwargs["stdout"] is call.kwargs["stderr"]
    assert bootstrap.marker_path(CAPTAIN).is_file()  # per-repo markers, both recorded
    assert bootstrap.marker_path(PRESENT).is_file()
    assert notice is not None and CAPTAIN in notice and PRESENT in notice  # the notice names both


# --- ordering: per-repo damping (case c) ---------------------------------------------


def test_narrow_attach_then_broad_attach_spawns_only_for_new_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known
    claude_present(monkeypatch)
    calls = capture_popen(monkeypatch)

    first = bootstrap.maybe_bootstrap([])  # only captain-hook required; records its marker, spawns for it
    assert first is not None
    assert worker_repos(calls[-1]) == [CAPTAIN]

    second = bootstrap.maybe_bootstrap([PRESENT])  # captain-hook damped by its fresh marker; cc-present fresh-absent
    assert second is not None
    assert worker_repos(calls[-1]) == [PRESENT]  # only cc-present spawns — the narrow attach did not damp it
    assert PRESENT in second and CAPTAIN not in second


# --- per-repo cooldown (case d) ------------------------------------------------------


def test_fresh_cc_present_marker_suppresses_only_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known
    claude_present(monkeypatch)
    bootstrap.record_attempt(bootstrap.marker_path(PRESENT), time.time())
    calls = capture_popen(monkeypatch)

    notice = bootstrap.maybe_bootstrap([PRESENT])

    assert len(calls) == 1
    assert worker_repos(calls[0]) == [CAPTAIN]  # cc-present damped by its fresh marker; captain-hook still spawns
    assert notice is not None and CAPTAIN in notice and PRESENT not in notice


def test_fresh_captain_marker_suppresses_respawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))
    claude_present(monkeypatch)
    bootstrap.record_attempt(bootstrap.marker_path(CAPTAIN), time.time())
    forbid_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap([]) is None  # within the hourly cooldown → no re-spawn


def test_expired_captain_marker_allows_respawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))
    claude_present(monkeypatch)
    bootstrap.record_attempt(bootstrap.marker_path(CAPTAIN), time.time() - bootstrap.RETRY_COOLDOWN_SECONDS - 1)
    calls = capture_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap([]) is not None
    assert len(calls) == 1  # a stale marker re-arms the spawn


def test_missing_claude_binary_records_attempt_and_skips_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))
    claude_absent(monkeypatch)
    forbid_popen(monkeypatch)
    assert bootstrap.maybe_bootstrap([]) is None
    assert bootstrap.marker_path(CAPTAIN).is_file()  # attempt recorded so it enters the cooldown, no busy re-check


def test_maybe_bootstrap_rechecks_known_under_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A sibling registers the marketplace between the lock-free outer check and lock acquisition;
    # the under-lock re-check catches it, so no worker spawns (pre-fix: no re-check → spawn).
    use_config(monkeypatch, seed_config(tmp_path))
    claude_present(monkeypatch)
    forbid_popen(monkeypatch)
    seen = iter([set(), {CAPTAIN}])
    monkeypatch.setattr(bootstrap, "known_repos", lambda: next(seen))
    assert bootstrap.maybe_bootstrap([]) is None


def test_marker_with_non_numeric_attempted_at_is_not_fresh(tmp_path: Path) -> None:
    # A hand-corrupted marker whose attempted_at isn't a number re-arms the attempt rather than
    # raising TypeError on the `now - attempted_at` subtraction.
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"attempted_at": "not-a-number"}))
    assert bootstrap.attempt_fresh(marker, time.time()) is False


# --- run_bootstrap worker (case e) ---------------------------------------------------


def test_run_bootstrap_adds_each_repo_no_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known
    claude_present(monkeypatch)  # which("claude") → the absolute path every call must carry
    calls = capture_run(monkeypatch)

    bootstrap.run_bootstrap(["a/b", "c/d"])

    assert [c.argv for c in calls] == [
        ["/usr/bin/claude", "plugin", "marketplace", "add", "a/b"],
        ["/usr/bin/claude", "plugin", "marketplace", "add", "c/d"],
    ]  # one marketplace-add per repo, in order — no `plugin install`
    assert all(c.kwargs["check"] is True and c.kwargs["timeout"] == 300 for c in calls)


def test_run_bootstrap_missing_claude_exits_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No claude on PATH: the worker resolves an absolute path first, logs, and exits without ever
    # invoking the literal "claude" (pre-fix it ran subprocess.run(["claude", ...])).
    use_config(monkeypatch, seed_config(tmp_path))
    claude_absent(monkeypatch)
    calls = capture_run(monkeypatch)
    bootstrap.run_bootstrap([CAPTAIN])
    assert calls == []


def test_run_bootstrap_skips_already_known_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path, CAPTAIN))
    claude_present(monkeypatch)
    calls = capture_run(monkeypatch)
    bootstrap.run_bootstrap([CAPTAIN])
    assert calls == []  # a sibling session registered the marketplace under the lock → no claude calls


# --- manifest field (case f) ---------------------------------------------------------


def load_manifest(tmp_path: Path, marketplaces: list[str]) -> manager.PackManifest:
    path = make_pack(tmp_path / "pk", "pk", marketplaces=marketplaces) / manager.PACK_MANIFEST
    return manager.PackManifest.load(path)


def test_manifest_loads_valid_marketplaces(tmp_path: Path) -> None:
    assert load_manifest(tmp_path, [PRESENT]).marketplaces == (PRESENT,)


def test_manifest_defaults_marketplaces_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path, []).marketplaces == ()


@pytest.mark.parametrize(
    "bad", ["--evil/x", "-x/y", "a/b/c", "nswedge"], ids=["flag-long", "flag-short", "two-slashes", "no-slash"]
)
def test_manifest_rejects_malformed_marketplace(tmp_path: Path, bad: str) -> None:
    with pytest.raises(manager.PackError, match="marketplace repo"):
        load_manifest(tmp_path, [bad])


# --- attach wiring (case g) ----------------------------------------------------------


def test_attach_with_extra_marketplace_spawns_only_for_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path, CAPTAIN))  # captain-hook known; cc-present unknown
    claude_present(monkeypatch)
    calls = capture_popen(monkeypatch)
    pack = make_pack(tmp_path / "ccx", "ccx", marketplaces=[PRESENT])

    result = CliRunner().invoke(cli, ["pack", "attach", str(pack)], input=json.dumps({"session_id": "sess-g"}))

    assert result.exit_code == 0, result.output
    recorded = manager.read_attached(ensure_session(SessionId("sess-g")))
    assert [p.name for p in recorded] == ["ccx"]  # the pack still lands
    assert len(calls) == 1
    assert worker_repos(calls[0]) == [PRESENT]  # captain-hook is silent (known); the declared extra spawns
    assert PRESENT in result.output and CAPTAIN not in result.output


def test_attach_survives_bootstrap_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, seed_config(tmp_path))
    pack = make_pack(tmp_path / "ccx", "ccx")

    def boom(*args: object, **kwargs: object) -> str | None:
        raise RuntimeError("bootstrap blew up")

    monkeypatch.setattr(bootstrap, "maybe_bootstrap", boom)
    result = CliRunner().invoke(cli, ["pack", "attach", str(pack)], input=json.dumps({"session_id": "sess-x"}))

    assert result.exit_code == 0, result.output  # fail-soft: the bootstrap error never breaks the attach
    assert "capt-hook: registering" not in result.output
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
        '  printf \'{"captain-hook": {"source": {"source": "github", "repo": "yasyf/captain-hook"}}}\''
        ' > "$CLAUDE_CONFIG_DIR/plugins/known_marketplaces.json"\n'
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
    assert first.stdout.strip() == bootstrap.bootstrap_notice([CAPTAIN])  # the one deliberate stdout

    def registered() -> bool:
        return known.exists() and CAPTAIN in known.read_text()

    deadline = time.time() + 30
    while time.time() < deadline and not registered():
        time.sleep(0.1)

    logged = argv_log.read_text() if argv_log.exists() else ""
    assert "plugin marketplace add yasyf/captain-hook" in logged
    assert "plugin install" not in logged  # the explicit install is dropped
    assert CAPTAIN in known.read_text()  # the worker registered the marketplace

    second = run_cli("pack", "attach", str(pack), stdin_data=json.dumps({"session_id": "sess-e2e-2"}), env=env)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""  # marketplace now known → hard no-op, no notice
