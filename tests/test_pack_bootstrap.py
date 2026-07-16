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


# --- known-marketplace detection (fix #1: all-source, not github-only) ----------------


def write_known(tmp_path: Path, payload: object) -> Path:
    """A CLAUDE_CONFIG_DIR whose known_marketplaces.json is exactly ``payload``."""
    config = tmp_path / "config"
    (config / "plugins").mkdir(parents=True, exist_ok=True)
    (config / "plugins" / "known_marketplaces.json").write_text(json.dumps(payload))
    return config


def is_known_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object, repo: str) -> bool:
    use_config(monkeypatch, write_known(tmp_path, payload))
    return bootstrap.is_known(repo, bootstrap.known_entries())


def test_github_source_repo_is_known(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {CAPTAIN: {"source": {"source": "github", "repo": CAPTAIN}}}
    assert is_known_in(monkeypatch, tmp_path, payload, CAPTAIN)


def test_github_slug_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {"c": {"source": {"source": "github", "repo": "Yasyf/Captain-Hook"}}}
    assert is_known_in(monkeypatch, tmp_path, payload, CAPTAIN)  # GitHub slugs are case-insensitive


def test_git_url_source_counts_as_known(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # captain-hook registered by git URL (not a github source) still counts as known; pre-fix the
    # github-only check missed it and re-added the marketplace hourly.
    payload = {"captain-hook": {"source": {"source": "git", "url": "https://github.com/yasyf/captain-hook.git"}}}
    assert is_known_in(monkeypatch, tmp_path, payload, CAPTAIN)


def test_local_source_matches_by_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a local source has no repo/url; the entry name equalling the repo basename is the only signal,
    # restoring the old name-membership coverage for un-repo-able sources.
    payload = {"captain-hook": {"source": {"source": "local", "path": "/x"}}}
    assert is_known_in(monkeypatch, tmp_path, payload, CAPTAIN)


def test_github_entry_does_not_leak_across_owners(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # bob/tools registered by github must NOT make alice/tools known: a github source is matched
    # precisely by repo and never falls back to the shared basename "tools".
    payload = {"tools": {"source": {"source": "github", "repo": "bob/tools"}}}
    assert not is_known_in(monkeypatch, tmp_path, payload, "alice/tools")


def test_source_less_entry_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a legacy source-less entry degrades to unknown (safe: spawn), even when its name matches.
    assert not is_known_in(monkeypatch, tmp_path, {"captain-hook": {}}, CAPTAIN)


def test_missing_known_marketplaces_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, tmp_path / "no-such-config")
    assert bootstrap.known_entries() == []


def test_malformed_known_marketplaces_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = seed_config(tmp_path)
    (config / "plugins" / "known_marketplaces.json").write_text("{ not json")
    use_config(monkeypatch, config)
    assert bootstrap.known_entries() == []


def test_non_object_known_marketplaces_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, write_known(tmp_path, [CAPTAIN]))
    assert bootstrap.known_entries() == []


def test_non_dict_entry_is_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_config(monkeypatch, write_known(tmp_path, {"captain-hook": "not-a-dict"}))
    assert bootstrap.known_entries() == []
    assert not bootstrap.is_known(CAPTAIN, bootstrap.known_entries())


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
    seen = iter([[], [("captain-hook", {"source": {"source": "github", "repo": CAPTAIN}})]])
    monkeypatch.setattr(bootstrap, "known_entries", lambda: next(seen))
    assert bootstrap.maybe_bootstrap([]) is None


def test_marker_with_non_numeric_attempted_at_is_not_fresh(tmp_path: Path) -> None:
    # A hand-corrupted marker whose attempted_at isn't a number re-arms the attempt rather than
    # raising TypeError on the `now - attempted_at` subtraction.
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"attempted_at": "not-a-number"}))
    assert bootstrap.attempt_fresh(marker, time.time()) is False


def test_marker_with_future_attempted_at_is_not_fresh(tmp_path: Path) -> None:
    # fix #8: a corrupt marker whose attempted_at is far in the future must re-arm the attempt, not
    # suppress it forever (pre-fix `now - future < COOLDOWN` was always true).
    marker = tmp_path / "marker.json"
    now = time.time()
    bootstrap.record_attempt(marker, now + bootstrap.RETRY_COOLDOWN_SECONDS * 10)
    assert bootstrap.attempt_fresh(marker, now) is False


def test_marker_path_is_keyed_on_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fix #4: two config dirs sharing a state dir must produce distinct markers for the same repo, or
    # one profile's marker damps another whose marketplace is genuinely unknown.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg-a"))
    first = bootstrap.marker_path(CAPTAIN)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg-b"))
    second = bootstrap.marker_path(CAPTAIN)
    assert first != second


def test_bootstrap_lock_held_returns_none_without_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fix #3: the worker holds the lock across up to K*300s of network calls; a sibling miss-path must
    # acquire non-blocking and return immediately rather than stall SessionStart to the hook timeout.
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known → miss path reaches the lock
    claude_present(monkeypatch)
    forbid_popen(monkeypatch)
    bootstrap.bootstrap_dir().mkdir(parents=True, exist_ok=True)
    held = bootstrap.FileLock(str(bootstrap.bootstrap_lock_path()))
    held.acquire()
    try:
        assert bootstrap.maybe_bootstrap([]) is None  # lock already held → immediate None, no spawn
    finally:
        held.release()


def test_hot_path_reads_known_marketplaces_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fix #5: the all-known hot path reads known_marketplaces.json once, not once per required repo.
    use_config(monkeypatch, seed_config(tmp_path, CAPTAIN, PRESENT, "yasyf/other"))
    forbid_popen(monkeypatch)
    reads = 0
    real = bootstrap.known_entries

    def counting() -> list[tuple[str, dict[str, object]]]:
        nonlocal reads
        reads += 1
        return real()

    monkeypatch.setattr(bootstrap, "known_entries", counting)
    assert bootstrap.maybe_bootstrap([PRESENT, "yasyf/other"]) is None
    assert reads == 1  # one read on the hot path, not three


def test_duplicate_marketplaces_deduped_in_worker_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fix #6: a manifest repeating an extra (or naming captain-hook explicitly) must not double the
    # worker argv or the notice.
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known
    claude_present(monkeypatch)
    calls = capture_popen(monkeypatch)
    notice = bootstrap.maybe_bootstrap([CAPTAIN, PRESENT, PRESENT])
    assert worker_repos(calls[0]) == [CAPTAIN, PRESENT]  # order-preserving dedupe, one of each
    assert notice is not None and notice.count(PRESENT) == 1


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


def test_run_bootstrap_continues_past_a_failing_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fix #2: a private/inaccessible first repo must not starve a valid later one — the raise is
    # logged and stepped over.
    use_config(monkeypatch, seed_config(tmp_path))  # nothing known
    claude_present(monkeypatch)
    attempted: list[str] = []

    def fake(argv: list[str], **kwargs: object) -> object:
        attempted.append(argv[-1])
        if argv[-1] == "a/b":
            raise subprocess.CalledProcessError(1, argv)
        return MagicMock(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    bootstrap.run_bootstrap(["a/b", "c/d"])
    assert attempted == ["a/b", "c/d"]  # the failing first repo did not block the valid second


def test_run_bootstrap_skips_unvalidated_argv_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fix #9: pack bootstrap takes repos straight from argv, bypassing load-time validation; a
    # flag-shaped slug is revalidated against MARKETPLACE_REPO_RE and never handed to marketplace-add.
    use_config(monkeypatch, seed_config(tmp_path))
    claude_present(monkeypatch)
    calls = capture_run(monkeypatch)
    bootstrap.run_bootstrap(["--evil/x"])
    assert calls == []  # no subprocess call issued for the unvalidated slug


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


def load_manifest_raw(tmp_path: Path, marketplaces_toml: str) -> manager.PackManifest:
    """Load a manifest whose ``marketplaces`` value is the given raw TOML (a non-list/non-string test)."""
    root = tmp_path / "pk"
    root.mkdir(parents=True, exist_ok=True)
    (root / manager.PACK_MANIFEST).write_text(
        f'name = "pk"\nversion = "0.1.0"\ndescription = "d"\nhooks = "."\nmarketplaces = {marketplaces_toml}\n'
    )
    return manager.PackManifest.load(root / manager.PACK_MANIFEST)


def test_manifest_non_string_marketplace_entry_raises_packerror(tmp_path: Path) -> None:
    # fix #7: a non-string entry must raise PackError (naming the value), not a bare TypeError out of
    # MARKETPLACE_REPO_RE.fullmatch — every PackError-shaped attach handler depends on it.
    with pytest.raises(manager.PackError, match="must be a string"):
        load_manifest_raw(tmp_path, "[5]")


def test_manifest_non_list_marketplaces_raises_packerror(tmp_path: Path) -> None:
    # fix #7: a scalar marketplaces value must raise PackError, not a TypeError out of tuple(5).
    with pytest.raises(manager.PackError, match="must be a list"):
        load_manifest_raw(tmp_path, '"oops"')


@pytest.mark.parametrize("slug", ["yasyf/café", "a/ŕ"], ids=["accented", "combining"])
def test_manifest_rejects_non_ascii_slug(tmp_path: Path, slug: str) -> None:
    # fix #10: ASCII-only MARKETPLACE_REPO_RE — a homoglyph/combining-mark slug can't pose as ASCII.
    with pytest.raises(manager.PackError, match="marketplace repo"):
        load_manifest(tmp_path, [slug])


def test_manifest_accepts_plain_ascii_slug(tmp_path: Path) -> None:
    assert load_manifest(tmp_path, [PRESENT]).marketplaces == (PRESENT,)  # fix #10: ASCII slug still valid


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
