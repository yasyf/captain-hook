from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from captain_hook import cli
from captain_hook.cli import CliState
from captain_hook.daemon import registry
from captain_hook.daemon.registry import Fingerprint, Registry
from captain_hook.packs import manager, plugins
from captain_hook.util.paths import resolve_claude_config_dir
from tests.helpers import make_project as scaffold

HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"
PLUGIN_HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='pp')\n"


def write_snapshot(root: Path, roster: list[tuple[str, str]]) -> Path:
    # Plant the discovery snapshot directly: the fingerprint reads it as-is (never refreshing via the
    # CLI), so no fake `claude` is needed — a roster is a list of (plugin_id, plugin_root) pairs.
    path = plugins.snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    plugins.PluginSnapshot(
        stat=plugins.fingerprint(root),
        plugins=tuple(plugins.EnabledPlugin(id=pid, version="1.0.0", root=proot) for pid, proot in roster),
    ).write(path)
    return path


def make_plugin_pack(pack_root: Path, *, hook_body: str = PLUGIN_HOOK, descriptor: str = "resources = []\n") -> Path:
    pack = pack_root / manager.PLUGIN_PACK_DIRNAME
    (hooks := pack / manager.HOOKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (guard := hooks / "guard.py").write_text(hook_body)
    (pack / manager.PACK_DESCRIPTOR).write_text(descriptor)
    return guard


@pytest.fixture(autouse=True)
def isolate_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, isolate_modules: None
) -> Iterator[None]:
    # discover() writes the resolve fastpath sidecar (and the plugin snapshot) under resolve_cache_dir();
    # keep it off the real ~/.cache. isolate_modules drops the per-test `hooks.*` imports so a later
    # project's discover can't reload a prior one's module from its stale spec under random ordering.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
    yield
    cli.register_pack_tools([])  # drop any tools this test registered into the process-global registry


def make_project(root: Path, *, hook_body: str = HOOK, gitignore: str | None = "*.log\n") -> CliState:
    scaffold(root, hook_body, gitignore=gitignore)
    return CliState(root=root, hooks=str(root / ".claude" / "hooks"))


@pytest.fixture
def project(tmp_path: Path) -> CliState:
    return make_project(tmp_path / "proj")


def fp(cli_state: CliState) -> Fingerprint:
    return Fingerprint.compute(cli_state)


# --- fingerprint invalidation matrix ---------------------------------------------------


def test_unchanged_tree_twice_is_equal(project: CliState) -> None:
    assert fp(project) == fp(project)
    assert fp(project).digest == fp(project).digest


def test_edit_hook_content_changes_fingerprint(project: CliState) -> None:
    before = fp(project)
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='a-much-longer-message'"))
    assert fp(project) != before


def test_add_file_changes_fingerprint(project: CliState) -> None:
    before = fp(project)
    (Path(project.hooks) / "extra.py").write_text("x = 1\n")
    assert fp(project) != before


def test_remove_file_changes_fingerprint(project: CliState) -> None:
    (Path(project.hooks) / "gone.py").write_text("y = 2\n")
    before = fp(project)
    (Path(project.hooks) / "gone.py").unlink()
    assert fp(project) != before


def test_language_marker_change_changes_fingerprint(project: CliState) -> None:
    # A new recursive build manifest flips builtin activation (go/python), so the fingerprint's
    # language-marker input must miss the cache.
    before = fp(project)
    (project.root / "go.mod").write_text("module x\n")
    assert fp(project) != before


def test_gitignore_change_changes_fingerprint(project: CliState) -> None:
    before = fp(project)
    (project.root / ".gitignore").write_text("*.log\n*.tmp\nbuild/\n")
    assert fp(project) != before


def test_snapshot_roster_change_changes_fingerprint(project: CliState) -> None:
    # A plugin enable/disable rewrites the discovery snapshot's roster; the fingerprint reads that
    # snapshot, so a roster change must miss the cache even when no watched settings file moved.
    write_snapshot(project.root, [("acme/one", "/nope")])
    before = fp(project)
    write_snapshot(project.root, [("acme/one", "/nope"), ("acme/two", "/nowhere")])
    assert fp(project) != before


def test_plugin_pack_hook_edit_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    # The roster is unchanged, but editing a hook file inside a discovered plugin pack must miss the
    # cache — the fingerprint digests each plugin pack's resolved hook tree, not just the snapshot roster.
    plugin_root = tmp_path / "plug"
    conf = make_plugin_pack(plugin_root)
    write_snapshot(project.root, [("acme/pp", str(plugin_root))])
    before = fp(project)
    conf.write_text(PLUGIN_HOOK.replace("message='pp'", "message='pp-edited-and-much-longer'"))
    assert fp(project) != before


def test_plugin_pack_descriptor_edit_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    # No hook file and no roster entry moved, but a descriptor-only edit (resources/tools) must miss the
    # cache — the fingerprint digests each plugin pack's pack.toml stat, not just its hook tree.
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    write_snapshot(project.root, [("acme/pp", str(plugin_root))])
    before = fp(project)
    (plugin_root / manager.PLUGIN_PACK_DIRNAME / manager.PACK_DESCRIPTOR).write_text(
        'resources = ["spacy:en_core_web_sm"]\n'
    )
    assert fp(project) != before


def test_malformed_plugin_pack_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    # The loader keys on the whole capt-hook/ dir — a pack.toml without a hooks/ dir is FATAL cold. The
    # fingerprint must digest the whole dir too, or warm silently ignores a pack cold crashes on.
    plugin_root = tmp_path / "plug"
    plugin_root.mkdir()  # an enabled plugin with no capt-hook/ yet — ships no pack
    write_snapshot(project.root, [("acme/pp", str(plugin_root))])
    before = fp(project)
    (pack := plugin_root / manager.PLUGIN_PACK_DIRNAME).mkdir()
    (pack / manager.PACK_DESCRIPTOR).write_text("resources = []\n")  # pack.toml but no hooks/ — malformed
    assert fp(project) != before  # the fingerprint now sees the malformed capt-hook/ dir
    assert [pid for pid, *_ in registry._plugin_trees(project.root)] == ["acme/pp"]  # not skipped


def test_plugin_tree_skips_a_plugin_whose_hooks_dir_vanishes(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dir vanishing mid-walk raises FileNotFoundError from the tree digest; the per-plugin fail-soft
    # try must skip that one plugin, never crash compute — the healthy sibling still contributes.
    good_root, bad_root = tmp_path / "good", tmp_path / "bad"
    make_plugin_pack(good_root)
    make_plugin_pack(bad_root)
    write_snapshot(project.root, [("acme/good", str(good_root)), ("acme/bad", str(bad_root))])
    real = registry._hooks_tree

    def flaky_tree(hooks: str) -> tuple[registry.HookEntry, ...]:
        if str(bad_root) in hooks:
            raise FileNotFoundError(hooks)  # the plugin dir vanished between the roster snapshot and the walk
        return real(hooks)

    monkeypatch.setattr(registry, "_hooks_tree", flaky_tree)
    assert [pid for pid, *_ in registry._plugin_trees(project.root)] == ["acme/good"]  # raising plugin skipped
    assert fp(project).digest  # compute did not raise


@pytest.mark.parametrize("idx", range(len(plugins.watched_paths(Path("/x")))))
def test_watched_file_mtime_bump_changes_fingerprint(project: CliState, idx: int) -> None:
    # A change to any watched roster input misses the cache — an mtime bump on the installed-plugin
    # roster, an enablement edit on a settings file. The absolute managed-settings system paths
    # (/Library, /etc) aren't sandbox-writable, so skip those slots.
    path = plugins.watched_paths(project.root)[idx]
    if not (path.is_relative_to(project.root) or path.is_relative_to(resolve_claude_config_dir())):
        pytest.skip(f"{path} is an absolute managed-settings system path — not sandbox-writable")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    before = fp(project)
    if path == plugins.installed_plugins_path():
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    else:
        path.write_text(json.dumps({"enabledPlugins": {"marker@mkt": True}}))
    assert fp(project) != before


def test_gitignore_preserved_mtime_rewrite_changes_fingerprint(project: CliState) -> None:
    # A same-size, mtime-preserved .gitignore rewrite only moves ctime; without ctime in the entry the
    # warm daemon would keep suppressing a hook cold now fires, so ctime must be part of the fingerprint.
    gitignore = project.root / ".gitignore"
    before = fp(project)
    st = gitignore.stat()
    gitignore.write_text("*.tmp\n")  # same byte length as the default "*.log\n", different content
    os.utime(gitignore, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
    after = gitignore.stat()
    assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns
    assert fp(project) != before


def test_pycache_fastpath_and_snapshot_do_not_invalidate(project: CliState) -> None:
    # A build writes __pycache__ into the hooks dir and a resolve sidecar into the cache; the
    # fingerprint must ignore the former and fold in the latter (and the plugin snapshot bracket) so
    # the very next call still hits.
    reg = Registry(project)
    first = reg.get()
    assert (Path(project.hooks) / "__pycache__").is_dir()
    assert reg.get() is first


# --- registry cache behaviour ----------------------------------------------------------


def test_cache_hit_returns_same_snapshot_object(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get()
    assert reg.get() is snap
    assert snap.resolved is not None
    assert snap.state.hooks, "discover populated the snapshot's state"


def test_edit_forces_rebuild_new_snapshot(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get()
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='changed-and-longer'"))
    rebuilt = reg.get()
    assert rebuilt is not snap
    assert rebuilt.fingerprint != snap.fingerprint


def test_drop_all_forces_rebuild(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get()
    reg.drop_all()
    assert reg.get() is not snap


def test_cache_hit_reconciles_stale_tool_registry(project: CliState) -> None:
    # A cache hit skips discover(); if a prior config left the process-global tool registry elsewhere,
    # serving the hit must reconcile it back to this snapshot's specs or the tool mis-lowers.
    from dataclasses import replace

    from cc_transcript.tools import expand_tool_names

    reg = Registry(project)
    base = reg.get()
    snap = replace(base, tools={"tool_a": ("Edit", None)})
    reg._cache[base.fingerprint] = snap
    cli.reconcile_pack_tools(snap.tools)
    assert "tool_a" in expand_tool_names("Edit")

    # A concurrent request for another config leaves the registry serving tool_b and drops tool_a.
    cli.reconcile_pack_tools({"tool_b": ("Write", None)})
    assert "tool_a" not in expand_tool_names("Edit")

    # Serving the still-cached snapshot reconciles the registry back to its own specs.
    assert reg.get() is snap
    assert "tool_a" in expand_tool_names("Edit")
    assert "tool_b" not in expand_tool_names("Write")


# --- concurrency: one build under contention -------------------------------------------


def test_build_never_serves_a_torn_discovery(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    # R4: a discovery that reads a hook file mid-rewrite (empty) must be discarded, not cached under
    # the now-complete file's fingerprint. The build retries and serves the settled discovery.
    reg = Registry(project)
    hook_file = Path(project.hooks) / "h.py"
    original = CliState.discover
    calls: list[int] = []

    def racing_discover(self: CliState) -> list[registry.manager.ResolvedPack]:
        calls.append(1)
        if len(calls) == 1:
            hook_file.write_text("")  # torn: discovery reads an empty, truncated hook file
            resolved = original(self)  # discovers nothing
            hook_file.write_text(HOOK)  # the rewrite completes right after the torn read
            return resolved
        return original(self)

    monkeypatch.setattr(CliState, "discover", racing_discover)
    snapshot = reg._build()
    assert len(calls) == 2, "the build did not retry after the tree moved during discovery"
    assert any(h.spec.message == "m" for h in snapshot.state.hooks), (
        "the served snapshot is the torn/empty intermediate — the project hook (message='m') is missing"
    )


def test_build_is_bounded_when_the_tree_keeps_moving(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    # R4: an endlessly-churning tree must not spin forever — retries cap at BUILD_RETRIES.
    digests = iter(str(n) for n in range(1000))
    monkeypatch.setattr(Fingerprint, "stable_digest", classmethod(lambda cls, cli: next(digests)))
    reg = Registry(project)
    calls: list[int] = []
    real_discover = reg._discover_once
    monkeypatch.setattr(reg, "_discover_once", lambda: (calls.append(1), real_discover())[1])
    result = reg._build()
    assert len(calls) == registry.BUILD_RETRIES, "the build was not bounded"
    assert result.state is not None
    assert result.cacheable is False, "an exhausted-retry (possibly torn) snapshot must not be cacheable"


def test_get_does_not_cache_a_non_cacheable_snapshot(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    # V9: a non-cacheable (exhausted/torn) snapshot is served but never stored — the next request rebuilds.
    from dataclasses import replace

    reg = Registry(project)
    builds: list[int] = []
    real = reg._discover_once
    monkeypatch.setattr(reg, "_discover_once", lambda: (builds.append(1), replace(real(), cacheable=False))[1])
    first = reg.get()
    reg.get()
    assert first.cacheable is False
    assert len(builds) == 2, "a non-cacheable snapshot was cached and served on the second request"
    assert any(h.spec.message == "m" for h in first.state.hooks), "the served snapshot is missing the project hook"


def test_concurrent_get_builds_once(project: CliState) -> None:
    reg = Registry(project)
    builds = 0
    lock = threading.Lock()
    original = reg._build

    def counting() -> registry.RegistrySnapshot:
        nonlocal builds
        with lock:
            builds += 1
        time.sleep(0.05)  # widen the window so peers pile up on the build lock
        return original()

    reg._build = counting
    start = threading.Barrier(12)

    def worker() -> registry.RegistrySnapshot:
        start.wait()
        return reg.get()

    with ThreadPoolExecutor(max_workers=12) as pool:
        snaps = [f.result() for f in [pool.submit(worker) for _ in range(12)]]

    assert builds == 1
    assert all(s is snaps[0] for s in snaps)


def test_a_roster_that_would_not_enumerate_is_served_but_never_cached(
    project: CliState, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A builtins-only answer is indistinguishable from a machine with no plugin packs. Caching one
    # would hold every plugin guard off until a watched input moves, long after `claude` recovered.
    calls: list[int] = []
    real = plugins.resolve_plugin_packs

    def flaky(root: Path) -> list[manager.ResolvedPack]:
        calls.append(1)
        if len(calls) == 1:
            raise plugins.PluginListError("claude plugin list exited 1")
        return real(root)

    monkeypatch.setattr(plugins, "resolve_plugin_packs", flaky)
    reg = Registry(project)

    failed = reg.get()
    assert not failed.cacheable
    assert any(e.source == cli.PLUGIN_ROSTER_SOURCE for e in failed.state.load_errors)

    recovered = reg.get()
    assert len(calls) == 2, "the second request served a cached failure instead of retrying"
    assert recovered.cacheable
