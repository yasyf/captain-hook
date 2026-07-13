from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from captain_hook.cli import CliState
from captain_hook.daemon import registry
from captain_hook.daemon.registry import Fingerprint, Registry

HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"
ATTACHED_HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='att')\n"


def make_attached_pack(pack_root: Path, session: Path, *, hook_body: str = ATTACHED_HOOK, name: str = "att") -> Path:
    (hooks := pack_root / "hooks").mkdir(parents=True, exist_ok=True)
    (conf := hooks / "conf.py").write_text(hook_body)
    (pack_root / "capt-hook.toml").write_text(
        f'name = "{name}"\ndescription = "attached test pack"\nhooks = "hooks"\nversion = "0.1.0"\n'
    )
    session.mkdir(parents=True, exist_ok=True)
    (session / "attached_packs.json").write_text(
        json.dumps([{"name": name, "dir": str(pack_root), "version": "0.1.0"}])
    )
    return conf


@pytest.fixture(autouse=True)
def isolate_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, isolate_modules: None
) -> None:
    # discover() writes the resolve fastpath sidecar under resolve_cache_dir(); keep it off the
    # real ~/.cache. isolate_modules drops the per-test `hooks.*` imports so a later project's
    # discover can't reload a prior one's module from its stale spec under random ordering.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(tmp_path_factory.mktemp("run")))


def make_project(
    root: Path, *, hook_body: str = HOOK, gitignore: str | None = "*.log\n", packs_toml: str | None = None
) -> CliState:
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "h.py").write_text(hook_body)
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore)
    if packs_toml is not None:
        (hooks / "packs.toml").write_text(packs_toml)
    return CliState(root=root, hooks=str(hooks))


@pytest.fixture
def project(tmp_path: Path) -> CliState:
    return make_project(tmp_path / "proj")


def fp(cli_state: CliState, session_dir: Path | None = None) -> Fingerprint:
    return Fingerprint.compute(cli_state, session_dir)


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


def test_packs_toml_change_changes_fingerprint(project: CliState) -> None:
    before = fp(project)
    (Path(project.hooks) / "packs.toml").write_text("[packs.general]\n")
    assert fp(project) != before


def test_gitignore_change_changes_fingerprint(project: CliState) -> None:
    before = fp(project)
    (project.root / ".gitignore").write_text("*.log\n*.tmp\nbuild/\n")
    assert fp(project) != before


def test_attached_json_change_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    before = fp(project, session)
    (session / "attached_packs.json").write_text(json.dumps([{"name": "p", "dir": "/nope", "version": "0.1.0"}]))
    assert fp(project, session) != before
    assert fp(project, session) != fp(project, None)


def test_attached_pack_hook_edit_changes_fingerprint(project: CliState, tmp_path: Path) -> None:
    # The attach set is unchanged, but editing a hook file inside an attached pack must miss the cache —
    # the fingerprint digests each attached pack's resolved hook tree, not just attached_packs.json.
    session = tmp_path / "session"
    conf = make_attached_pack(tmp_path / "attpack", session)
    before = fp(project, session)
    conf.write_text(ATTACHED_HOOK.replace("message='att'", "message='att-edited-and-longer'"))
    assert fp(project, session) != before


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


def test_pycache_and_fastpath_do_not_invalidate(project: CliState) -> None:
    # A build writes __pycache__ into the hooks dir and a resolve sidecar into the cache; the
    # fingerprint must ignore the former and fold in the latter so the very next call still hits.
    reg = Registry(project)
    first = reg.get(None)
    assert (Path(project.hooks) / "__pycache__").is_dir()
    assert reg.get(None) is first


# --- registry cache behaviour ----------------------------------------------------------


def test_cache_hit_returns_same_snapshot_object(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get(None)
    assert reg.get(None) is snap
    assert snap.resolved is not None
    assert snap.state.hooks, "discover populated the snapshot's state"


def test_edit_forces_rebuild_new_snapshot(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get(None)
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='changed-and-longer'"))
    rebuilt = reg.get(None)
    assert rebuilt is not snap
    assert rebuilt.fingerprint != snap.fingerprint


def test_two_attach_sets_produce_two_snapshots(project: CliState, tmp_path: Path) -> None:
    sess_a, sess_b = tmp_path / "a", tmp_path / "b"
    sess_a.mkdir()
    sess_b.mkdir()
    (sess_a / "attached_packs.json").write_text(json.dumps([{"name": "pa", "dir": "/x", "version": "0.1.0"}]))
    (sess_b / "attached_packs.json").write_text(json.dumps([{"name": "pb", "dir": "/y", "version": "0.1.0"}]))

    reg = Registry(project)
    snap_a, snap_b = reg.get(sess_a), reg.get(sess_b)

    assert snap_a is not snap_b
    assert snap_a.fingerprint != snap_b.fingerprint
    assert reg.get(sess_a) is snap_a


def test_drop_all_forces_rebuild(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get(None)
    reg.drop_all()
    assert reg.get(None) is not snap


def test_snapshot_past_horizon_is_a_miss(project: CliState) -> None:
    reg = Registry(project)
    snap = reg.get(None)
    stale = Fingerprint(digest=snap.fingerprint.digest, horizon=time.time() - 1)
    reg._cache[stale] = registry.RegistrySnapshot(stale, snap.state, snap.resolved)
    assert stale.expired(time.time())
    assert reg._lookup(stale, time.time()) is None


# --- concurrency: one build under contention -------------------------------------------


def test_concurrent_get_builds_once(project: CliState) -> None:
    reg = Registry(project)
    builds = 0
    lock = threading.Lock()
    original = reg._build

    def counting(session_dir: Path | None) -> registry.RegistrySnapshot:
        nonlocal builds
        with lock:
            builds += 1
        time.sleep(0.05)  # widen the window so peers pile up on the build lock
        return original(session_dir)

    reg._build = counting
    start = threading.Barrier(12)

    def worker() -> registry.RegistrySnapshot:
        start.wait()
        return reg.get(None)

    with ThreadPoolExecutor(max_workers=12) as pool:
        snaps = [f.result() for f in [pool.submit(worker) for _ in range(12)]]

    assert builds == 1
    assert all(s is snaps[0] for s in snaps)
