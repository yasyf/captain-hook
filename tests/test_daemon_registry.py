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
from tests.helpers import make_project as scaffold
from tests.helpers import plant_roster

HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"
PLUGIN_HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='pp')\n"


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


def test_unchanged_root_walks_for_language_markers_once(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    walks: list[Path] = []
    real = manager.detect_languages
    monkeypatch.setattr(manager, "detect_languages", lambda root: (walks.append(root), real(root))[1])
    reg = Registry(project)
    reg.get()
    walks.clear()
    reg.get()
    reg.get()
    assert walks == []


def test_nested_language_marker_lands_within_the_ttl(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    (nested := project.root / "services" / "api").mkdir(parents=True)
    before = fp(project)
    (nested / "go.mod").write_text("module x\n")
    assert fp(project) == before
    monkeypatch.setattr(registry, "MARKER_TTL", 0.0)
    assert fp(project) != before


def test_a_build_is_keyed_by_the_languages_its_discovery_saw(
    project: CliState, monkeypatch: pytest.MonkeyPatch
) -> None:
    (nested := project.root / "services" / "api").mkdir(parents=True)
    reg = Registry(project)
    assert "go" not in {p.name for p in reg.get().resolved}
    (marker := nested / "go.mod").write_text("module x\n")
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='edited'"))
    assert "go" in {p.name for p in reg.get().resolved}
    marker.unlink()
    monkeypatch.setattr(registry, "MARKER_TTL", 0.0)
    assert "go" not in {p.name for p in reg.get().resolved}


def test_gitignore_change_changes_fingerprint(project: CliState) -> None:
    before = fp(project)
    (project.root / ".gitignore").write_text("*.log\n*.tmp\nbuild/\n")
    assert fp(project) != before


def test_roster_change_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    (one := tmp_path / "one").mkdir()
    (two := tmp_path / "two").mkdir()
    plant_roster([("acme/one", one)])
    before = fp(project)
    plant_roster([("acme/one", one), ("acme/two", two)])
    assert fp(project) != before


def test_install_dir_vanishing_behind_an_unchanged_roster_lands_within_the_ttl(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    before = fp(project)
    (moved := tmp_path / "moved").mkdir()
    plugin_root.rename(moved / "plug")
    assert fp(project) == before
    monkeypatch.setattr(registry, "PLUGIN_TTL", 0.0)
    assert fp(project) != before


def test_project_disable_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    before = fp(project)
    (project.root / ".claude" / "settings.local.json").write_text(json.dumps({"enabledPlugins": {"acme/pp": False}}))
    assert fp(project) != before


def test_plugin_pack_hook_edit_lands_within_the_ttl(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plug"
    conf = make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    before = fp(project)
    conf.write_text(PLUGIN_HOOK.replace("message='pp'", "message='pp-edited-and-much-longer'"))
    assert fp(project) == before
    assert Fingerprint.compute(project, fresh=True) != before
    monkeypatch.setattr(registry, "PLUGIN_TTL", 0.0)
    assert fp(project) != before


def test_plugin_pack_hook_edit_reaches_the_served_snapshot_within_the_ttl(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plug"
    conf = make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    reg = Registry(project)
    snap = reg.get()
    conf.write_text(PLUGIN_HOOK.replace("message='pp'", "message='pp-edited'"))
    assert reg.get() is snap
    monkeypatch.setattr(registry, "PLUGIN_TTL", 0.0)
    assert any(h.spec.message == "pp-edited" for h in reg.get().state.hooks)


def test_local_hook_edit_reaches_the_next_snapshot(project: CliState, tmp_path: Path) -> None:
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    reg = Registry(project)
    reg.get()
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='edited'"))
    assert any(h.spec.message == "edited" for h in reg.get().state.hooks)


def test_concurrent_fingerprints_walk_each_input_once(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    marker_walks: list[Path] = []
    roster_reads: list[Path] = []
    real_detect, real_roster = manager.detect_languages, plugins.enabled_plugins

    def slow_detect(root: Path) -> set[str]:
        marker_walks.append(root)
        time.sleep(0.05)
        return real_detect(root)

    def slow_roster(root: Path) -> tuple[plugins.EnabledPlugin, ...]:
        roster_reads.append(root)
        time.sleep(0.05)
        return real_roster(root)

    monkeypatch.setattr(manager, "detect_languages", slow_detect)
    monkeypatch.setattr(plugins, "enabled_plugins", slow_roster)
    start = threading.Barrier(12)

    def worker() -> Fingerprint:
        start.wait()
        return fp(project)

    with ThreadPoolExecutor(max_workers=12) as pool:
        prints = [f.result() for f in [pool.submit(worker) for _ in range(12)]]

    assert len(marker_walks) == 1
    assert len(roster_reads) == 1
    assert len(set(prints)) == 1


def test_plugin_pack_descriptor_edit_changes_fingerprint(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No hook file and no roster entry moved, but a descriptor-only edit (resources/tools) must miss the
    # cache — the fingerprint digests each plugin pack's pack.toml stat, not just its hook tree.
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    before = fp(project)
    monkeypatch.setattr(registry, "PLUGIN_TTL", 0.0)
    (plugin_root / manager.PLUGIN_PACK_DIRNAME / manager.PACK_DESCRIPTOR).write_text(
        'resources = ["spacy:en_core_web_sm"]\n'
    )
    assert fp(project) != before


def test_malformed_plugin_pack_changes_fingerprint(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loader keys on the whole capt-hook/ dir — a pack.toml without a hooks/ dir is FATAL cold. The
    # fingerprint must digest the whole dir too, or warm silently ignores a pack cold crashes on.
    plugin_root = tmp_path / "plug"
    plugin_root.mkdir()  # an enabled plugin with no capt-hook/ yet — ships no pack
    plant_roster([("acme/pp", plugin_root)])
    before = fp(project)
    monkeypatch.setattr(registry, "PLUGIN_TTL", 0.0)
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
    plant_roster([("acme/good", good_root), ("acme/bad", bad_root)])
    real = registry._hooks_tree

    def flaky_tree(hooks: str) -> tuple[registry.HookEntry, ...]:
        if str(bad_root) in hooks:
            raise FileNotFoundError(hooks)
        return real(hooks)

    monkeypatch.setattr(registry, "_hooks_tree", flaky_tree)
    assert [pid for pid, *_ in registry._plugin_trees(project.root)] == ["acme/good"]  # raising plugin skipped
    assert fp(project).digest  # compute did not raise


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


def test_a_build_does_not_invalidate_its_own_fingerprint(project: CliState) -> None:
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
    monkeypatch.setattr(Fingerprint, "compute", classmethod(lambda cls, cli, *, fresh=False: cls(next(digests))))
    reg = Registry(project)
    calls: list[int] = []
    real_discover = reg._discover_once
    monkeypatch.setattr(reg, "_discover_once", lambda fingerprint: (calls.append(1), real_discover(fingerprint))[1])
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
    monkeypatch.setattr(
        reg, "_discover_once", lambda fingerprint: (builds.append(1), replace(real(fingerprint), cacheable=False))[1]
    )
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
            raise plugins.PluginListError("plugin roster unreadable")
        return real(root)

    monkeypatch.setattr(plugins, "resolve_plugin_packs", flaky)
    reg = Registry(project)

    failed = reg.get()
    assert not failed.cacheable
    assert any(e.source == cli.PLUGIN_ROSTER_SOURCE for e in failed.state.load_errors)

    recovered = reg.get()
    assert len(calls) == 2, "the second request served a cached failure instead of retrying"
    assert recovered.cacheable
