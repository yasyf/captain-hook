from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

import pytest

from captain_hook.packs import manager, plugins
from captain_hook.util.paths import resolve_claude_config_dir
from tests.helpers import run_cli

HOOK_SRC = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message={message!r})\n"


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    # Snapshot writes root under resolve_cache_dir(); conftest isolates CLAUDE_CONFIG_DIR and the
    # state dir but not XDG_CACHE_HOME, so keep the .plugins sidecar off the real ~/.cache here
    # (the pattern in tests/test_daemon_registry.py).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))


# --- helpers -------------------------------------------------------------------------


def write_plugin_pack(
    tmp: Path,
    name: str,
    version: str = "1.0.0",
    *,
    slot: str | None = None,
    repository: str | None = None,
    hook_message: str | None = None,
    descriptor: str = "resources = []\n",
) -> Path:
    """A versioned cache-shaped plugin root shipping a pack at the fixed ``capt-hook/`` path."""
    root = tmp / "install" / (slot or name) / version
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    plugin_json: dict[str, object] = {
        "name": name,
        "version": version,
        "dependencies": [{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=11.0.0"}],
    }
    if repository is not None:
        plugin_json["repository"] = repository
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(plugin_json))
    (hooks := root / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / manager.PLUGIN_PACK_DIRNAME / manager.PACK_DESCRIPTOR).write_text(descriptor)
    (hooks / "h.py").write_text(HOOK_SRC.format(message=hook_message or name))
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


# --- fixed-path resolution over the roster -------------------------------------------


def test_enabled_plugin_with_pack_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show", repository="https://github.com/yasyf/show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("show@show", plugin)])

    (rp,) = plugins.resolve_plugin_packs(root)
    assert isinstance(rp.entry, manager.PluginPack)
    assert (rp.entry.plugin_id, rp.entry.root, rp.entry.repository) == (
        "show@show",
        str(plugin),
        "https://github.com/yasyf/show",
    )
    assert rp.pack_id == "plugin:show@show"
    assert rp.name == "show@show"
    assert rp.path == plugin / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME


def test_namespaced_identity_orders_by_plugin_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    a = write_plugin_pack(tmp_path, "a", slot="a")
    b = write_plugin_pack(tmp_path, "b", slot="b")
    # Deliberately out of id order in the roster; resolution sorts by full plugin id.
    install_claude(
        tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("b@mkt", b), roster_entry("a@mkt", a)]
    )
    assert [rp.pack_id for rp in plugins.resolve_plugin_packs(root)] == ["plugin:a@mkt", "plugin:b@mkt"]


def test_plugin_without_repository_routes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")  # plugin.json declares no repository
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("show@show", plugin)])
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.entry.repository is None


def test_disabled_plugin_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(
        tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("show@show", plugin, enabled=False)]
    )

    assert plugins.enabled_plugins(root) == ()  # the CLI's enabled filter drops it
    assert plugins.resolve_plugin_packs(root) == []


def test_plugin_without_pack_dir_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A plugin that ships no capt-hook/ dir merely consumes packs (or none) and is skipped silently.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (install := tmp_path / "install" / "consumer" / "1.0.0").mkdir(parents=True)
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("consumer@mkt", install)])
    assert plugins.resolve_plugin_packs(root) == []


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda pack: (pack / manager.PACK_DESCRIPTOR).unlink(), id="missing_descriptor"),
        pytest.param(lambda pack: shutil.rmtree(pack / manager.HOOKS_DIRNAME), id="missing_hooks_dir"),
        pytest.param(lambda pack: (pack / manager.PACK_DESCRIPTOR).write_text("name = = broken\n"), id="broken_toml"),
        pytest.param(
            lambda pack: (pack / manager.PACK_DESCRIPTOR).write_text("[tools.x]\n"), id="tool_missing_behaves_like"
        ),
        pytest.param(
            lambda pack: (pack / manager.PACK_DESCRIPTOR).write_text('resources = "not-a-list"\n'), id="bad_resources"
        ),
    ],
)
def test_malformed_pack_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mangle) -> None:  # type: ignore[no-untyped-def]
    # All-or-nothing: a plugin advertising capt-hook/ with a malformed pack raises, never silently
    # dropping guards — and the healthy sibling never papers over it.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    good = write_plugin_pack(tmp_path, "good", slot="good")
    bad = write_plugin_pack(tmp_path, "bad", slot="bad")
    mangle(bad / manager.PLUGIN_PACK_DIRNAME)
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[roster_entry("good@mkt", good), roster_entry("bad@mkt", bad)],
    )
    with pytest.raises(manager.PackError):
        plugins.resolve_plugin_packs(root)


def test_duplicate_roster_entries_deduped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    a = write_plugin_pack(tmp_path, "a", slot="a")
    b = write_plugin_pack(tmp_path, "b", slot="b")
    # Claude Code re-lists an enabled plugin under each marketplace/ref, so the same id can appear
    # several times; the roster dedupes by id and one plugin id yields exactly one pack.
    install_claude(
        tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("dup@mkt", a), roster_entry("dup@mkt", b)]
    )
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.pack_id == "plugin:dup@mkt"


# --- snapshot & CLI invocation -------------------------------------------------------


def test_two_calls_run_cli_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    first, second = plugins.enabled_plugins(root), plugins.enabled_plugins(root)
    assert first == second and len(first) == 1
    assert calls.read_text() == "x"  # the second event served the snapshot, no re-spawn


@pytest.mark.parametrize("idx", range(len(plugins.watched_paths(Path("/x")))))
def test_refresh_on_each_watched_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idx: int) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
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
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
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
    good = write_plugin_pack(tmp_path, "show", "1.0.0")
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
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
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
    v1 = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", v1, version="1.0.0")])

    (first,) = plugins.resolve_plugin_packs(root)
    assert first.entry.root == str(v1)

    # A plugin update writes a new versioned cache root and bumps the roster; touching a watched file
    # invalidates the snapshot so the next event re-runs the CLI and rebinds to the new root.
    v2 = write_plugin_pack(tmp_path, "show", "2.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", v2, version="2.0.0")])
    plugins.installed_plugins_path().write_text("x" * 500)

    (second,) = plugins.resolve_plugin_packs(root)
    assert second.entry.root == str(v2)
    assert second.path == v2 / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME


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
    plugin = write_plugin_pack(tmp_path, "show")  # ships hook(Event.PreToolUse, message="show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    out = run_dispatch(root, "PreToolUse", tool_name="Bash", tool_input={"command": "echo hi"})
    assert "show" in out  # the discovered plugin pack's hook fired
    assert out.count("show") == 1  # exactly once — discovery loaded it a single time


def test_e2e_discovered_plugin_pack_fires_for_subagent_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery is per project, not per session, so a subagent-originated event loads the pack too.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    out = run_dispatch(root, "PreToolUse", tool_name="Bash", tool_input={"command": "echo hi"}, agent_id="sub-1")
    assert "show" in out


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
    plugin = write_plugin_pack(tmp_path, "redpack")
    (plugin / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME / "h.py").write_text(
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
