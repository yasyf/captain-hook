from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
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


def roster_entry(
    plugin_id: str,
    root: Path,
    *,
    version: str = "1.0.0",
    enabled: bool = True,
    scope: str = "user",
    project_path: Path | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": plugin_id,
        "version": version,
        "enabled": enabled,
        "installPath": str(root),
        "scope": scope,
    }
    if project_path is not None:
        entry["projectPath"] = str(project_path)
    return entry


def fake_claude(
    bindir: Path,
    roster: list[dict[str, object]] | None = None,
    *,
    calls: Path,
    returncode: int = 0,
    raw_stdout: str | None = None,
    delay: float = 0.0,
) -> Path:
    """An executable ``claude`` shim: appends to ``calls`` per invocation and prints the roster JSON."""
    bindir.mkdir(parents=True, exist_ok=True)
    payload = raw_stdout if raw_stdout is not None else json.dumps(roster or [])
    (script := bindir / "claude").write_text(
        f"#!{sys.executable}\n"
        "import sys, pathlib, time\n"
        f"pathlib.Path({str(calls)!r}).open('a').write('x')\n"
        f"time.sleep({delay!r})\n"
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
    delay: float = 0.0,
) -> None:
    bindir = fake_claude(tmp / "bin", roster, calls=calls, returncode=returncode, raw_stdout=raw_stdout, delay=delay)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def settle_refresh(timeout: float = 20.0) -> None:
    """Wait out any background roster refresh, so an assertion sees the settled snapshot."""
    deadline = time.monotonic() + timeout
    while plugins.REFRESHING and time.monotonic() < deadline:
        time.sleep(0.01)


def enable_marker(value: str) -> str:
    return json.dumps({"enabledPlugins": {"marker@mkt": value}})


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


def test_parse_plugin_entry_retains_scope() -> None:
    entry = plugins.parse_plugin_entry({"id": "a/b", "enabled": True, "installPath": "/p", "scope": "project"})
    assert entry is not None and entry.scope == "project"


def test_identical_scoped_roster_entries_collapse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude re-lists one install under both project and user scope; identical (id, path) collapses to one.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[roster_entry("show@mkt", plugin, scope="project"), roster_entry("show@mkt", plugin, scope="user")],
    )
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.pack_id == "plugin:show@mkt"


def test_scope_precedence_project_over_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The same id installed at two paths across scopes resolves by precedence: project outranks user.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    proj = write_plugin_pack(tmp_path, "show", "9.9.9", slot="proj")
    user = write_plugin_pack(tmp_path, "show", "1.0.0", slot="user")
    # user listed first — precedence, not roster order, decides.
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[roster_entry("show@mkt", user, scope="user"), roster_entry("show@mkt", proj, scope="project")],
    )
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.entry.root == str(proj)  # the project-scope install wins


def test_same_scope_conflict_in_one_project_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two install paths at the same scope OF THE SAME PROJECT is a corrupt roster: it raises, and the
    # failure is never cached as if the machine simply had no plugin packs.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    a = write_plugin_pack(tmp_path, "show", slot="a")
    b = write_plugin_pack(tmp_path, "show", slot="b")
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[
            roster_entry("show@mkt", a, scope="project", project_path=root),
            roster_entry("show@mkt", b, scope="project", project_path=root),
        ],
    )
    with pytest.raises(plugins.PluginListError, match="2 install paths at scope 'project'"):
        plugins.enabled_plugins(root)
    assert not plugins.snapshot_path(root).exists()


def test_same_scope_in_different_projects_resolves_per_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The live P1: one plugin id installed at `local` scope in several projects is normal, not corrupt.
    # projectPath is what tells the installs apart, and each root resolves to its own.
    plant_installed()
    (here := tmp_path / "here").mkdir()
    (there := tmp_path / "there").mkdir()
    mine = write_plugin_pack(tmp_path, "codex", "1.9.0", slot="mine")
    theirs = write_plugin_pack(tmp_path, "codex", "1.8.4", slot="theirs")
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[
            roster_entry("codex@skills", theirs, version="1.8.4", scope="local", project_path=there),
            roster_entry("codex@skills", mine, version="1.9.0", scope="local", project_path=here),
        ],
    )
    (ours,) = plugins.resolve_plugin_packs(here)
    assert ours.entry.root == str(mine)
    (yours,) = plugins.resolve_plugin_packs(there)
    assert yours.entry.root == str(theirs)


def test_foreign_project_entry_does_not_govern_this_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A project-scoped install belongs to its own project: it neither loads here nor outranks the
    # user-scope entry that does.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (other := tmp_path / "other").mkdir()
    ours = write_plugin_pack(tmp_path, "show", slot="ours")
    foreign = write_plugin_pack(tmp_path, "show", slot="foreign")
    only_theirs = write_plugin_pack(tmp_path, "theirs", slot="theirs")
    install_claude(
        tmp_path,
        monkeypatch,
        calls=tmp_path / "calls",
        roster=[
            roster_entry("show@mkt", ours, scope="user"),
            roster_entry("show@mkt", foreign, scope="local", project_path=other),
            roster_entry("theirs@mkt", only_theirs, scope="project", project_path=other),
        ],
    )
    (resolved,) = plugins.resolve_plugin_packs(root)
    assert resolved.entry.root == str(ours)


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


@pytest.mark.parametrize("stale_plugins", [pytest.param([], id="poisoned-empty"), pytest.param(None, id="populated")])
def test_pre_scoping_snapshot_is_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_plugins: list[object] | None
) -> None:
    # Neither a pre-scoping roster nor the empty one the old failure path wrote moves the stat tuple,
    # so the format version is what invalidates them.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("show@mkt", plugin)])

    plugins.enabled_plugins(root)
    snapshot = plugins.snapshot_path(root)
    stale = json.loads(snapshot.read_text())
    del stale["version"]
    for entry in stale["plugins"]:
        del entry["project_path"]
    if stale_plugins is not None:
        stale["plugins"] = stale_plugins
    snapshot.write_text(json.dumps(stale))

    assert plugins.PluginSnapshot.load(snapshot) is None
    (only,) = plugins.enabled_plugins(root)
    assert only.id == "show@mkt"
    assert calls.read_text() == "xx"  # the pre-scoping snapshot was discarded and the CLI re-asked


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
    watched.write_text("x" * 500 if watched == plugins.installed_plugins_path() else enable_marker("on"))
    plugins.enabled_plugins(root)
    settle_refresh()
    plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"  # the watched-file change re-ran the CLI


def test_mtime_restored_enablement_rewrite_refreshes_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An enable/disable can land as a same-size rewrite with mtime restored, moving neither size nor
    # mtime; the digest reads the key itself, so it is caught however the file was written.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(enable_marker("on_"))
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    plugins.enabled_plugins(root)
    assert calls.read_text() == "x"
    st = settings.stat()
    settings.write_text(enable_marker("off"))  # same byte length, different enablement
    os.utime(settings, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = settings.stat()
    assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns
    plugins.enabled_plugins(root)
    settle_refresh()
    plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"  # the enablement change re-ran the CLI


def test_settings_rewrite_that_cannot_change_enablement_serves_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of the digest: a session rewriting its settings for reasons that have nothing to
    # do with plugins used to cost every root a Node spawn.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"model": "opus", "enabledPlugins": {"marker@mkt": "on"}}))
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])

    plugins.enabled_plugins(root)
    assert calls.read_text() == "x"
    settings.write_text(json.dumps({"model": "sonnet", "effortLevel": "high", "enabledPlugins": {"marker@mkt": "on"}}))
    plugins.enabled_plugins(root)
    settle_refresh()
    plugins.enabled_plugins(root)
    assert calls.read_text() == "x"  # never re-ran: enablement is untouched


def test_unparseable_settings_still_invalidate_coarsely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A file the digest cannot read falls back to its stat, so a corrupt settings file never reads as
    # "declares no plugins" and quietly pins a stale roster.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not json")
    before = plugins.fingerprint(root)
    settings.write_text("{still not json, but longer}")
    assert plugins.fingerprint(root) != before


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"returncode": 1}, id="nonzero_exit"),
        pytest.param({"raw_stdout": "not json", "returncode": 0}, id="bad_json"),
    ],
)
def test_cli_failure_raises_and_is_never_cached_as_a_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: dict[str, object]
) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[], **failure)

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    assert not plugins.snapshot_path(root).exists()  # a failure is never cached as an authoritative roster
    assert plugins.failure_path(root).exists()
    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    assert calls.read_text() == "x"  # the recorded failure is re-raised rather than re-spawning the CLI


def test_empty_roster_is_cached_as_success_not_as_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[])

    assert plugins.enabled_plugins(root) == ()
    assert plugins.snapshot_path(root).exists()
    assert not plugins.failure_path(root).exists()
    assert plugins.enabled_plugins(root) == ()
    assert calls.read_text() == "x"


def test_recorded_failure_expires_with_its_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[], returncode=1)

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    recorded = plugins.RosterFailure.load(plugins.failure_path(root))
    assert recorded is not None
    aged = plugins.RosterFailure(
        stat=recorded.stat, at=recorded.at - plugins.FAILURE_TTL - timedelta(seconds=1), message=recorded.message
    )
    aged.write(plugins.failure_path(root))

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"  # the expired record re-asks


def test_recorded_failure_is_retried_when_a_watched_file_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[], returncode=1)

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    (settings := root / ".claude" / "settings.json").parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    assert calls.read_text() == "xx"  # a settings change retries at once rather than after the TTL


def test_recovered_roster_clears_the_recorded_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[], returncode=1)

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    assert plugins.failure_path(root).exists()

    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls2", roster=[roster_entry("mkt/show", plugin)])
    (settings := root / ".claude" / "settings.json").parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")

    assert len(plugins.enabled_plugins(root)) == 1
    assert not plugins.failure_path(root).exists()


def test_dead_shebang_claude_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # R1: a `claude` with a dead shebang passes shutil.which but fails exec with OSError.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (bindir := tmp_path / "bin").mkdir(parents=True)
    (script := bindir / "claude").write_text("#!/nonexistent/interpreter\nprint('unreachable')\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(plugins.PluginListError, match="could not be executed"):
        plugins.enabled_plugins(root)
    assert not plugins.snapshot_path(root).exists()


def test_roster_failure_is_recorded_where_a_person_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap
) -> None:  # type: ignore[no-untyped-def]
    # Discovery keeps serving the builtins, and the failure reaches the status dashboard's load
    # errors and the fault store the next session start drains.
    from captain_hook import faults
    from captain_hook.app import _state
    from captain_hook.cli import PLUGIN_ROSTER_SOURCE, CliState

    plant_installed()
    (root := tmp_path / "proj").mkdir()
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[], returncode=1)

    packs = CliState(root=root).discover()

    assert packs and all(isinstance(p.entry, manager.BuiltinPack) for p in packs)
    (error,) = _state.load_errors
    assert error.source == PLUGIN_ROSTER_SOURCE and isinstance(error.exc, plugins.PluginListError)
    assert [r for r in logcap.records if r.levelno == logging.ERROR and "plugin roster unusable" in r.message]
    (line,) = faults.drain()
    assert line.startswith(faults.ANNOUNCE_PREFIX) and PLUGIN_ROSTER_SOURCE in line


def test_managed_settings_dropin_changes_invalidate_snapshot(tmp_path: Path) -> None:
    # R5: a managed-settings.d drop-in under the config dir is watched — add/edit/remove of a *.json
    # drop-in moves the stat tuple, invalidating a stale roster snapshot.
    (root := tmp_path / "proj").mkdir()
    dropin = resolve_claude_config_dir() / "managed-settings.d"
    absent = plugins.fingerprint(root)

    dropin.mkdir(parents=True)
    (drop := dropin / "10-policy.json").write_text(enable_marker("on"))
    added = plugins.fingerprint(root)
    assert added != absent  # a new drop-in moves the fingerprint

    drop.write_text(enable_marker("off"))
    assert plugins.fingerprint(root) != added  # editing a drop-in moves it

    drop.unlink()
    assert plugins.fingerprint(root) != added  # removing the drop-in moves it back off


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
def test_non_list_roster_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_stdout: str) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", raw_stdout=raw_stdout)
    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)
    assert not plugins.snapshot_path(root).exists()


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


def test_claude_off_path_raises_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude Code is installed (installed_plugins.json exists) but its CLI is unreachable — the state
    # a daemon's launchd PATH produces. Unenumerable is not the same answer as "no plugins".
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    (empty := tmp_path / "empty-bin").mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no claude reachable

    with pytest.raises(plugins.PluginListError, match="not on PATH"):
        plugins.enabled_plugins(root)
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

    plugins.enabled_plugins(root)
    settle_refresh()
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


def test_pack_list_renders_builtin_and_plugin_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("show@mkt", plugin)])

    result = run_cli("pack", "list", root_dir=str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "builtin:general" in result.stdout  # an active wheel builtin renders with its namespaced id
    assert "plugin:show@mkt" in result.stdout  # the enabled plugin's pack renders with its full plugin id


def test_gate_bounds_concurrent_roots_under_one_invalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    roots = [tmp_path / f"proj{i}" for i in range(10)]
    for root in roots:
        root.mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    overlap = 0
    live = 0
    real = plugins.list_plugins_cli

    def watched(root: Path, executable: str) -> tuple[plugins.EnabledPlugin, ...]:
        nonlocal overlap, live
        live += 1
        overlap = max(overlap, live)
        try:
            return real(root, executable)
        finally:
            live -= 1

    monkeypatch.setattr(plugins, "list_plugins_cli", watched)
    errors: list[BaseException] = []

    def enumerate_root(root: Path) -> None:
        try:
            plugins.enabled_plugins(root)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=enumerate_root, args=(root,)) for root in roots]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert overlap <= plugins.GATE_WIDTH
    assert overlap < len(roots)


def test_gate_never_shares_one_root_enablement_with_another(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `enabled` resolves against each project's settings stack, so it differs between roots.
    plant_installed()
    (enabled_root := tmp_path / "on").mkdir()
    (disabled_root := tmp_path / "off").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")

    def per_root(root: Path, executable: str) -> tuple[plugins.EnabledPlugin, ...]:
        del executable
        return (plugins.EnabledPlugin(id="mkt/show", version="1.0.0", root=str(plugin), scope="user"),) * (
            root.resolve() == enabled_root.resolve()
        )

    monkeypatch.setattr(plugins, "list_plugins_cli", per_root)
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    assert len(plugins.enabled_plugins(enabled_root)) == 1
    assert plugins.enabled_plugins(disabled_root) == ()
    assert len(plugins.enabled_plugins(enabled_root)) == 1


def test_gate_held_by_a_dead_holder_does_not_wedge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flock is released by the kernel when its holder dies, so a killed process frees its slot.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugins.gate_path(0).parent.mkdir(parents=True, exist_ok=True)
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time\n"
            "handles = [open(p, 'w') for p in sys.argv[1:]]\n"
            "for h in handles: fcntl.flock(h, fcntl.LOCK_EX)\n"
            "time.sleep(300)",
            *[str(plugins.gate_path(slot)) for slot in range(plugins.GATE_WIDTH)],
        ],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(plugins.gate_path(s).exists() for s in range(plugins.GATE_WIDTH)):
            time.sleep(0.05)
        holder.kill()
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:
            holder.kill()

    assert len(plugins.enabled_plugins(root)) == 1


def test_spawn_outage_is_recorded_machine_wide_and_spares_queued_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_installed()
    (first := tmp_path / "one").mkdir()
    (second := tmp_path / "two").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    spawns = 0

    def exhausted(root: Path, executable: str) -> tuple[plugins.EnabledPlugin, ...]:
        del root, executable
        nonlocal spawns
        spawns += 1
        raise plugins.RosterUnavailable(
            "claude plugin list could not be executed: [Errno 35] Resource temporarily unavailable"
        )

    monkeypatch.setattr(plugins, "list_plugins_cli", exhausted)

    with pytest.raises(plugins.RosterUnavailable):
        plugins.enabled_plugins(first)
    with pytest.raises(plugins.RosterUnavailable):
        plugins.enabled_plugins(second)
    assert spawns == 1
    assert plugins.outage_path().exists()


def test_roster_shape_failure_stays_local_to_its_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only an unspawnable CLI is a machine condition; an unusable answer is about that root alone.
    plant_installed()
    (broken := tmp_path / "broken").mkdir()
    (healthy := tmp_path / "healthy").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    real = plugins.list_plugins_cli

    def selective(root: Path, executable: str) -> tuple[plugins.EnabledPlugin, ...]:
        if root.resolve() == broken.resolve():
            raise plugins.PluginListError("claude plugin list returned unparseable JSON")
        return real(root, executable)

    monkeypatch.setattr(plugins, "list_plugins_cli", selective)
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])

    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(broken)
    assert not plugins.outage_path().exists()
    assert len(plugins.enabled_plugins(healthy)) == 1


def test_recovered_spawn_clears_the_machine_wide_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install_claude(tmp_path, monkeypatch, calls=tmp_path / "calls", roster=[roster_entry("mkt/show", plugin)])
    plugins.outage_path().parent.mkdir(parents=True, exist_ok=True)
    plugins.RosterOutage(
        at=datetime.now(UTC) - plugins.FAILURE_TTL - timedelta(seconds=1), message="stale outage"
    ).write(plugins.outage_path())

    assert len(plugins.enabled_plugins(root)) == 1
    assert not plugins.outage_path().exists()


def test_gate_admits_exactly_its_width_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del tmp_path, monkeypatch
    plugins.gate_path(0).parent.mkdir(parents=True, exist_ok=True)
    inside = threading.Semaphore(0)
    release = threading.Event()

    def hold() -> None:
        with plugins.roster_gate():
            inside.release()
            release.wait(timeout=30)

    holders = [threading.Thread(target=hold, daemon=True) for _ in range(plugins.GATE_WIDTH)]
    for thread in holders:
        thread.start()
    for _ in range(plugins.GATE_WIDTH):
        assert inside.acquire(timeout=10)

    entered = threading.Event()

    def overflow() -> None:
        with plugins.roster_gate():
            entered.set()

    extra = threading.Thread(target=overflow, daemon=True)
    extra.start()
    assert not entered.wait(timeout=1)

    release.set()
    assert entered.wait(timeout=10)
    for thread in [*holders, extra]:
        thread.join(timeout=10)


def test_root_with_no_snapshot_blocks_on_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing to serve, and an empty roster is the forbidden collapse — a first-ever root must wait.
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)], delay=1.0)

    started = time.monotonic()
    (only,) = plugins.enabled_plugins(root)
    assert time.monotonic() - started >= 1.0  # it waited for the answer rather than guessing
    assert only.id == "mkt/show"
    assert calls.read_text() == "x"


def test_stale_snapshot_is_served_without_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    v1 = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", v1)])
    (first,) = plugins.enabled_plugins(root)

    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(enable_marker("flipped"))
    v2 = write_plugin_pack(tmp_path, "other", "1.0.0", slot="other")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/other", v2)], delay=2.0)

    started = time.monotonic()
    (served,) = plugins.enabled_plugins(root)
    assert time.monotonic() - started < 1.0  # the stale snapshot came back, the 2s spawn ran behind it
    assert served.id == first.id == "mkt/show"

    settle_refresh()
    (refreshed,) = plugins.enabled_plugins(root)
    assert refreshed.id == "mkt/other"  # one event later, the refresh has landed


def test_background_refresh_does_not_stack_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])
    plugins.enabled_plugins(root)

    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(enable_marker("flipped"))
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)], delay=1.0)
    for _ in range(5):
        plugins.enabled_plugins(root)
    settle_refresh()
    assert calls.read_text() == "xx"  # five stale reads, one refresh spawn


def test_failed_background_refresh_records_and_stops_respawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_installed()
    (root := tmp_path / "proj").mkdir()
    calls = tmp_path / "calls"
    plugin = write_plugin_pack(tmp_path, "show", "1.0.0")
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[roster_entry("mkt/show", plugin)])
    plugins.enabled_plugins(root)

    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(enable_marker("flipped"))
    install_claude(tmp_path, monkeypatch, calls=calls, roster=[], returncode=1)
    for _ in range(4):
        plugins.enabled_plugins(root)
        settle_refresh()

    assert plugins.failure_path(root).exists()  # the failure was recorded, not swallowed
    assert calls.read_text() == "xx"  # and the record stopped the retries
    assert plugins.enabled_plugins(root)  # the stale snapshot is still served throughout
