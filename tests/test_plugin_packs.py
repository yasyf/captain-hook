from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from captain_hook.packs import bootstrap, manager, plugins
from captain_hook.util.paths import resolve_claude_config_dir
from tests.helpers import run_cli


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    # Snapshot writes root under resolve_cache_dir(); conftest isolates CLAUDE_CONFIG_DIR and the
    # state dir but not XDG_CACHE_HOME, so keep the .plugins sidecar off the real ~/.cache here
    # (the pattern in tests/test_daemon_registry.py).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))


# --- helpers -------------------------------------------------------------------------


def pack_manifest(name: str, version: str, *, hooks: str = ".") -> str:
    return f'[pack]\nname = "{name}"\ndescription = "d"\nhooks = "{hooks}"\nversion = "{version}"\n'


def write_plugin_dir(
    tmp: Path, name: str, version: str, *, slot: str | None = None, manifest_name: str | None = None, hooks: str = "."
) -> Path:
    """A versioned cache-shaped plugin root carrying a ``[pack]`` manifest and one hook file."""
    root = tmp / "install" / (slot or name) / version
    root.mkdir(parents=True, exist_ok=True)
    (root / manager.PACK_MANIFEST).write_text(pack_manifest(manifest_name or name, version, hooks=hooks))
    (hooks_dir := (root if hooks == "." else root / hooks)).mkdir(parents=True, exist_ok=True)
    (hooks_dir / "h.py").write_text(
        f"from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message={(manifest_name or name)!r})\n"
    )
    return root


def roster_entry(plugin_id: str, root: Path, *, version: str = "1.0.0", enabled: bool = True) -> dict[str, object]:
    return {"id": plugin_id, "version": version, "enabled": enabled, "installPath": str(root)}


def fake_claude(
    bindir: Path,
    roster: list[dict[str, object]] | None = None,
    *,
    calls: Path,
    returncode: int = 0,
    raw_stdout: str | None = None,
) -> Path:
    """An executable ``claude`` shim: appends to ``calls`` per invocation and prints the roster JSON."""
    bindir.mkdir(parents=True, exist_ok=True)
    payload = raw_stdout if raw_stdout is not None else json.dumps(roster or [])
    (script := bindir / "claude").write_text(
        f"#!{sys.executable}\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(calls)!r}).open('a').write('x')\n"
        f"sys.stdout.write({payload!r})\n"
        f"sys.exit({returncode})\n"
    )
    script.chmod(0o755)
    return bindir


def install_claude(
    tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: Path,
    roster: list[dict[str, object]] | None = None,
    returncode: int = 0,
    raw_stdout: str | None = None,
) -> None:
    bindir = fake_claude(tmp / "bin", roster, calls=calls, returncode=returncode, raw_stdout=raw_stdout)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def plant_installed() -> None:
    # conftest's CLAUDE_CONFIG_DIR seeds plugins/known_marketplaces.json but not installed_plugins.json;
    # planting it flips the existence gate so discovery runs, all under the isolated config dir.
    (path := plugins.installed_plugins_path()).parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")


# --- resolution over the roster ------------------------------------------------------


def test_enabled_plugin_with_pack_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    (rp,) = plugins.resolve_plugin_packs(root, set())
    assert isinstance(rp.entry, manager.PluginPack)
    assert (rp.entry.name, rp.entry.plugin_id, rp.entry.dir, rp.entry.version) == (
        "show",
        "mkt/show",
        str(plugin),
        "1.0.0",
    )
    assert rp.path == plugin  # hooks = "." -> hooks_dir is the pack root
    assert rp.manifest.name == "show"


def test_hooks_subdir_layout_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "under", "1.0.0", hooks="hooks")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/under", plugin)])

    (rp,) = plugins.resolve_plugin_packs(root, set())
    assert rp.entry.dir == str(plugin)
    assert rp.path == plugin / "hooks"


def test_split_file_layout_resolves_hooks_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # R8 repro: a consumer-only .claude/capt-hook.toml at the plugin root shadows nothing — the real
    # [pack] lives one hooks/ level down, and discovery must resolve it there.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (install := tmp_path / "install" / "consumer" / "1.0.0").mkdir(parents=True)
    (install / ".claude").mkdir(parents=True)
    (install / ".claude" / manager.PACK_MANIFEST).write_text("[packs.general]\n")  # consumer-only
    (hooks := install / "hooks").mkdir(parents=True)
    (hooks / manager.PACK_MANIFEST).write_text(pack_manifest("nested", "1.0.0"))
    (hooks / "h.py").write_text("from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='nested')\n")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/consumer", install)])

    (rp,) = plugins.resolve_plugin_packs(root, set())
    assert rp.manifest.name == "nested"
    assert rp.path == hooks  # the hooks/ manifest won, not the consumer .claude file


def test_disabled_plugin_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(
        tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin, enabled=False)]
    )

    assert plugins.enabled_plugins(root) == ()  # the CLI's enabled filter drops it
    assert plugins.resolve_plugin_packs(root, set()) == []


@pytest.mark.parametrize(
    "manifest_text",
    [
        pytest.param(None, id="no_manifest_file"),
        pytest.param("[packs.general]\n", id="consumer_only"),
        pytest.param("", id="empty_file"),
    ],
)
def test_non_pack_plugin_skipped_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap, manifest_text: str | None
) -> None:  # type: ignore[no-untyped-def]
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (install := tmp_path / "install" / "consumer" / "1.0.0").mkdir(parents=True)
    if manifest_text is not None:
        (install / manager.PACK_MANIFEST).write_text(manifest_text)
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/consumer", install)])

    assert plugins.resolve_plugin_packs(root, set()) == []  # a plugin that merely consumes packs is not a pack
    assert not [r for r in logcap.records if "malformed" in r.message]  # silent, not fail-soft


@pytest.mark.parametrize(
    "bad_manifest",
    [
        pytest.param('[pack]\nname = "bad"\n', id="missing_keys_packerror"),
        pytest.param("name = = broken\n", id="broken_toml"),
        pytest.param('[pack]\nname = 5\ndescription = "d"\nhooks = "."\n', id="wrong_type_packerror"),
    ],
)
def test_malformed_pack_fail_soft_sibling_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap, bad_manifest: str
) -> None:  # type: ignore[no-untyped-def]
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    good = write_plugin_dir(tmp_path, "good", "1.0.0")
    (bad := tmp_path / "install" / "bad" / "1.0.0").mkdir(parents=True)
    (bad / manager.PACK_MANIFEST).write_text(bad_manifest)
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[roster_entry("mkt/good", good), roster_entry("mkt/bad", bad)],
    )

    assert [rp.entry.name for rp in plugins.resolve_plugin_packs(root, set())] == ["good"]
    assert [r.message for r in logcap.records if "malformed" in r.message]  # the rot logged a debug, dispatch survived


def test_declared_name_shadows_plugin_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap) -> None:  # type: ignore[no-untyped-def]
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    # "show" is a [packs.show] name the project declares — even an uncached/unreachable external that
    # never resolves. The old attach shadowing bug let the plugin pack leak through; declared names now
    # shadow regardless of whether the external is cached.
    assert plugins.resolve_plugin_packs(root, {"show"}) == []
    assert [r for r in logcap.records if "shadowed" in r.message]


def test_duplicate_names_lower_id_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap) -> None:  # type: ignore[no-untyped-def]
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    p1 = write_plugin_dir(tmp_path, "dup", "1.0.0", slot="one")
    p2 = write_plugin_dir(tmp_path, "dup", "1.0.0", slot="two")
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[roster_entry("zeta/p", p2), roster_entry("alpha/p", p1)],  # deliberately out of id order
    )

    (rp,) = plugins.resolve_plugin_packs(root, set())
    assert rp.entry.plugin_id == "alpha/p"  # first by plugin id wins
    assert rp.entry.dir == str(p1)
    (warn,) = [r for r in logcap.records if "duplicate plugin pack" in r.message]
    assert warn.levelno == logging.WARNING
    assert "alpha/p" in warn.message and "zeta/p" in warn.message  # both ids named


# --- snapshot & CLI invocation -------------------------------------------------------


def test_two_calls_run_cli_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    first, second = plugins.enabled_plugins(root), plugins.enabled_plugins(root)
    assert first == second and len(first) == 1
    assert calls.read_text() == "x"  # the second event served the snapshot, no re-spawn


@pytest.mark.parametrize("idx", range(len(plugins.watched_paths(Path("/x")))))
def test_refresh_on_each_watched_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idx: int) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    plugins.enabled_plugins(root)
    assert calls.read_text() == "x"
    watched = plugins.watched_paths(root)[idx]
    if not (watched.is_relative_to(root) or watched.is_relative_to(resolve_claude_config_dir())):
        pytest.skip(f"{watched} is an absolute managed-settings system path — not sandbox-writable")
    watched.parent.mkdir(parents=True, exist_ok=True)
    watched.write_text("x" * 500)  # absent -> present, or a size bump: either moves the stat tuple
    plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"  # the watched-file change re-ran the CLI


def test_ctime_only_watched_rewrite_refreshes_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A same-size, mtime-restored rewrite of a watched settings file moves only ctime; without ctime in
    # the stat record the snapshot would stay "fresh" and miss a plugin enable/disable landing that way.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"a": 1}')
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    plugins.enabled_plugins(root)
    assert calls.read_text() == "x"
    st = settings.stat()
    settings.write_text('{"b": 2}')  # same byte length, different content
    os.utime(settings, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime; only ctime moves
    after = settings.stat()
    assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns
    plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"  # ctime move re-ran the CLI


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"returncode": 1}, id="nonzero_exit"),
        pytest.param({"raw_stdout": "not json", "returncode": 0}, id="bad_json"),
    ],
)
def test_cli_failure_caches_empty_roster_and_damps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap, failure: dict[str, object]
) -> None:  # type: ignore[no-untyped-def]
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[], **failure)

    assert plugins.enabled_plugins(root) == ()
    assert plugins.snapshot_path(root).is_file()  # an empty-roster snapshot was written
    assert plugins.enabled_plugins(root) == ()
    assert calls.read_text() == "x"  # a broken CLI is not re-invoked per event
    assert [r for r in logcap.records if r.levelno == logging.WARNING and "claude plugin list failed" in r.message]

    plugins.installed_plugins_path().write_text("x" * 500)  # a watched-file change re-triggers
    plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"


def test_dead_shebang_claude_caches_empty_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap) -> None:  # type: ignore[no-untyped-def]
    # R1: a `claude` with a dead shebang passes shutil.which but fails exec with OSError — that must
    # damp to an empty snapshot, not crash dispatch.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (bindir := tmp_path / "bin").mkdir(parents=True)
    (script := bindir / "claude").write_text("#!/nonexistent/interpreter\nprint('unreachable')\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    assert plugins.enabled_plugins(root) == ()
    snap = plugins.PluginSnapshot.load(plugins.snapshot_path(root))
    assert snap is not None and snap.plugins == ()  # empty-roster snapshot written, no exception escaped
    assert [r for r in logcap.records if r.levelno == logging.WARNING and "claude plugin list failed" in r.message]


def test_managed_settings_dropin_changes_invalidate_snapshot(tmp_path: Path) -> None:
    # R5: a managed-settings.d drop-in under the config dir is watched — add/edit/remove of a *.json
    # drop-in moves the stat tuple, invalidating a stale roster snapshot.
    (root := tmp_path / "proj").mkdir()
    dropin = resolve_claude_config_dir() / "managed-settings.d"
    absent = plugins.stat_records(root)

    dropin.mkdir(parents=True)
    (drop := dropin / "10-policy.json").write_text('{"a": 1}')
    added = plugins.stat_records(root)
    assert added != absent  # a new drop-in moves the stat tuple

    drop.write_text('{"policy": "changed and now longer"}')  # different byte length
    assert plugins.stat_records(root) != added  # editing a drop-in moves it

    drop.unlink()
    assert plugins.stat_records(root) != added  # removing the drop-in moves it back off


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        pytest.param(
            {"id": "a/b", "version": "1", "enabled": True, "installPath": "/p"}, ("a/b", "1", "/p"), id="valid"
        ),
        pytest.param(123, None, id="numeric-entry"),
        pytest.param("x", None, id="string-entry"),
        pytest.param({"enabled": True, "installPath": "/p"}, None, id="missing-id"),
        pytest.param({"id": "a/b", "enabled": True}, None, id="missing-installpath"),
        pytest.param({"id": 9, "enabled": True, "installPath": "/p"}, None, id="non-string-id"),
        pytest.param({"id": "a/b", "enabled": True, "installPath": 7}, None, id="non-string-installpath"),
        pytest.param({"id": "a/b", "enabled": False, "installPath": "/p"}, None, id="not-enabled"),
        pytest.param(
            {"id": "a/b", "version": 9, "enabled": True, "installPath": "/p"},
            ("a/b", "", "/p"),
            id="non-string-version",
        ),
    ],
)
def test_parse_plugin_entry(entry: object, expected: tuple[str, str, str] | None) -> None:
    result = plugins.parse_plugin_entry(entry)
    assert (result and (result.id, result.version, result.root)) == expected


@pytest.mark.parametrize(
    "raw_stdout",
    [pytest.param('{"id": "x", "enabled": true}', id="top-level-dict"), pytest.param("42", id="numeric-top-level")],
)
def test_non_list_roster_degrades_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_stdout: str) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", raw_stdout=raw_stdout)
    assert plugins.enabled_plugins(root) == ()  # a non-array roster is a PluginListError, damped to empty
    assert plugins.snapshot_path(root).is_file()  # an empty-roster snapshot was written, so it damps


def test_malformed_entry_does_not_suppress_valid_siblings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    good = write_plugin_dir(tmp_path, "show", "1.0.0")
    roster = [
        roster_entry("mkt/show", good),  # valid
        123,  # a bare number, not an object
        {"enabled": True, "installPath": str(good)},  # missing id
        {"id": "mkt/other", "enabled": True},  # enabled but no installPath
    ]
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", raw_stdout=json.dumps(roster))
    (only,) = plugins.enabled_plugins(root)  # exactly the one valid sibling survives
    assert only.id == "mkt/show"


def test_installed_plugins_absent_runs_no_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    # installed_plugins.json is deliberately not planted; the existence gate returns before any spawn.
    assert plugins.enabled_plugins(root) == ()
    assert not calls.exists()
    assert not plugins.snapshot_path(root).exists()  # and no snapshot is written


def test_claude_off_path_returns_empty_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (empty := tmp_path / "empty-bin").mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no claude reachable

    assert plugins.enabled_plugins(root) == ()
    assert not plugins.snapshot_path(root).exists()


def test_newer_plugin_version_rebinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    v1 = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", v1, version="1.0.0")])

    (first,) = plugins.resolve_plugin_packs(root, set())
    assert (first.entry.dir, first.entry.version) == (str(v1), "1.0.0")

    # A plugin update writes a new versioned cache root and bumps the roster; touching a watched file
    # invalidates the snapshot so the next event re-runs the CLI and rebinds to the new root.
    v2 = write_plugin_dir(tmp_path, "show", "2.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", v2, version="2.0.0")])
    plugins.installed_plugins_path().write_text("x" * 500)

    (second,) = plugins.resolve_plugin_packs(root, set())
    assert (second.entry.dir, second.entry.version) == (str(v2), "2.0.0")
    assert second.path == v2


# --- end-to-end dispatch (the discovered plugin pack's hook fires) -------------------


def run_dispatch(root: Path, event: str, **raw: object) -> str:
    """Dispatch ``event`` over a subprocess CLI run — the fake claude/env are already on os.environ."""
    stdin = json.dumps({"session_id": "e2e-sess", **raw})
    result = run_cli("run", event, root_dir=str(root), stdin_data=stdin)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_e2e_discovered_plugin_pack_hook_fires_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")  # ships hook(Event.PreToolUse, message="show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    out = run_dispatch(root, "PreToolUse", tool_name="Bash", tool_input={"command": "echo hi"})
    assert "show" in out  # the discovered plugin pack's hook fired
    assert out.count("show") == 1  # exactly once — discovery loaded it a single time


def test_e2e_discovered_plugin_pack_fires_for_subagent_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery is per project, not per session, so a subagent-originated event loads the pack too.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    out = run_dispatch(root, "PreToolUse", tool_name="Bash", tool_input={"command": "echo hi"}, agent_id="sub-1")
    assert "show" in out


# --- discover-tail bootstrap (a discovered pack's declared marketplaces) --------------


def discover(root: Path, hooks: Path) -> None:
    from captain_hook.cli import CliState

    CliState(root=root, hooks=str(hooks)).discover()


def declare_marketplaces(plugin: Path, name: str, version: str, marketplaces: list[str]) -> None:
    mkt = f"marketplaces = {json.dumps(marketplaces)}\n"
    (plugin / manager.PACK_MANIFEST).write_text(pack_manifest(name, version) + mkt)


def test_discover_tail_bootstraps_declared_marketplaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # spawn_worker is the capture boundary — not subprocess.Popen, which list_plugins_cli shares.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")
    declare_marketplaces(plugin, "show", "1.0.0", ["yasyf/cc-present"])
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    spawned: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "spawn_worker", lambda repos: spawned.append(list(repos)))

    discover(root, tmp_path / "no-hooks")

    assert spawned == [["yasyf/cc-present"]]  # exactly one worker, for the declared marketplace
    assert capsys.readouterr() == ("", "")  # discovery's bootstrap tail prints nothing on any stream


def test_discover_no_marketplaces_is_zero_marketplace_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_dir(tmp_path, "show", "1.0.0")  # declares no marketplaces
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    def no_spawn(_: object) -> None:
        raise AssertionError("no worker should spawn when no pack declares a marketplace")

    def no_read() -> list[tuple[str, dict[str, object]]]:
        raise AssertionError("known_marketplaces.json must not be read for an empty union")

    monkeypatch.setattr(bootstrap, "spawn_worker", no_spawn)
    monkeypatch.setattr(bootstrap, "known_entries", no_read)

    discover(root, tmp_path / "no-hooks")
    assert not bootstrap.bootstrap_dir().exists()  # zero marketplace I/O — no marker dir created


# --- test-subcommand scoping (plugin packs are never swept in) ------------------------


def test_test_subcommand_never_resolves_plugin_packs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolate_modules: None
) -> None:
    # `capt-hook test` never sweeps in plugin packs nor spawns `claude plugin list`.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (local_hooks := root / ".claude" / "hooks").mkdir(parents=True)
    (local_hooks / "__init__.py").write_text("")
    (local_hooks / "good.py").write_text(
        "from captain_hook.app import hook\n"
        "from captain_hook.types import Event\n"
        "from captain_hook.testing.types import Block, Input\n\n"
        'hook(Event.PreToolUse, message="ok", block=True, tests={Input(command="echo hi"): Block()})\n'
    )
    plugin = write_plugin_dir(tmp_path, "redpack", "1.0.0")
    (plugin / "h.py").write_text(
        "from captain_hook.app import hook\n"
        "from captain_hook.types import Event\n"
        "from captain_hook.testing.types import Allow, Input\n\n"
        'hook(Event.PreToolUse, message="RED", block=True, tests={Input(command="echo hi"): Allow()})\n'
    )
    calls = tmp_path / "calls"
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/redpack", plugin)])

    result = run_cli("test", root_dir=str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 tests: 1 passed" in result.stdout
    assert "RED" not in result.stdout  # the plugin pack was not tested
    assert not calls.exists()  # claude plugin list was never spawned

    from captain_hook.cli import CliState

    CliState(root=root).discover()  # full-scope control DOES resolve the plugin pack
    assert calls.exists()
