from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import Timeout
from pydantic import ValidationError

from captain_hook import cli
from captain_hook.cli import CliState
from captain_hook.daemon import registry
from captain_hook.daemon.registry import Fingerprint, Registry
from captain_hook.packs import manager, plugins
from captain_hook.util import reqenv
from tests.helpers import make_project as scaffold
from tests.helpers import plant_roster

HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"
PLUGIN_HOOK = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='pp')\n"
WORKERS = 8
SPAWN = multiprocessing.get_context("spawn")


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
    registry.HOOK_TREES.cache_clear()
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


def record(ledger: Path, event: str) -> None:
    with ledger.open("a") as out:
        out.write(f"{event} {os.getpid()}\n")


def ledger_pids(ledger: Path, event: str) -> list[str]:
    return [pid for kind, pid in (line.split() for line in ledger.read_text().splitlines()) if kind == event]


def await_ledger(ledger: Path, event: str, count: int, procs: list[BaseProcess]) -> None:
    deadline = time.monotonic() + 120
    while len(set(ledger_pids(ledger, event))) < count:
        assert all(proc.is_alive() for proc in procs), f"a worker exited before recording {event}"
        assert time.monotonic() < deadline, f"only {ledger_pids(ledger, event)} recorded {event}"
        time.sleep(0.05)


def spawn(target: Callable[..., None], *args: object) -> BaseProcess:
    (proc := SPAWN.Process(target=target, args=args, daemon=True)).start()
    return proc


def cold_registry_languages(root: Path, ledger: Path, results: Queue[list[str]]) -> None:
    real_detect, real_clock = manager.detect_languages, time.monotonic

    def walk(path: Path) -> frozenset[str]:
        record(ledger, "walk")
        return real_detect(path)

    def clock() -> float:
        now = real_clock()
        record(ledger, "clock")
        return now

    manager.detect_languages = walk
    registry.time = SimpleNamespace(monotonic=clock)
    snapshot = Registry(CliState(root=root, hooks=str(root / ".claude" / "hooks"))).get()
    results.put(sorted({pack.name for pack in snapshot.resolved} & manager.LANGUAGE_MARKERS.keys()))


def stalled_walk(root: Path, ledger: Path) -> None:
    def stall(path: Path) -> frozenset[str]:
        record(ledger, "walk")
        time.sleep(600)
        raise AssertionError("the walker outlived its kill")

    manager.detect_languages = stall
    registry._language_markers(root, since=time.monotonic())


def state_env() -> dict[str, str]:
    return {"CAPTAIN_HOOK_STATE_DIR": os.environ["CAPTAIN_HOOK_STATE_DIR"]}


def generation(cli_state: CliState) -> registry.LanguageGeneration:
    return registry.LanguageGeneration.model_validate_json(registry.marker_path(cli_state.root).read_text())


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1_000.0]
    monkeypatch.setattr(registry, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []
    real = manager.detect_languages
    monkeypatch.setattr(manager, "detect_languages", lambda root: (seen.append(root), real(root))[1])
    return seen


# --- fingerprint invalidation matrix ---------------------------------------------------


def test_unchanged_tree_twice_is_equal(project: CliState) -> None:
    assert fp(project) == fp(project)
    assert fp(project).digest == fp(project).digest


def test_registry_build_marks_the_bound_request_warm_up(project: CliState) -> None:
    reg = Registry(project)
    requests = [
        reqenv.RequestOverrides(env=state_env(), cwd=str(project.root), client_ppid=1, session_id="s") for _ in range(2)
    ]
    for overrides in requests:
        with reqenv.use_request(overrides):
            reg.get()

    assert [overrides.warmups for overrides in requests] == [["registry"], []]


def test_warm_registry_avoids_hook_directory_enumeration(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = Path(project.hooks)
    (hooks / "nested").mkdir()
    (hooks / "nested" / "data.txt").write_text("one")
    reg = Registry(project)
    snapshot = reg.get()
    assert snapshot.state.registry_fingerprint == snapshot.fingerprint.digest
    scans: list[Path] = []
    original = os.scandir

    def counted(path):
        if Path(path).is_relative_to(hooks):
            scans.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", counted)
    for _ in range(10):
        assert reg.get() is snapshot
    assert scans == []
    (hooks / "nested" / "data.txt").write_text("changed")
    assert fp(project) != snapshot.fingerprint
    assert scans == []
    (hooks / "nested" / "data.txt").unlink()
    assert fp(project) != snapshot.fingerprint
    assert scans == [hooks / "nested"]


def test_fresh_fingerprints_enumerate_local_and_plugin_hooks(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    expected = fp(project)
    roots = {Path(project.hooks), plugin_root / manager.PLUGIN_PACK_DIRNAME}
    scans: list[Path] = []
    original = os.scandir

    def counted(path):
        if Path(path) in roots:
            scans.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", counted)
    assert Fingerprint.compute(project, fresh=True) == expected
    assert set(scans) == roots


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


def test_cold_registry_walks_for_language_markers_once(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    walks: list[Path] = []
    real = manager.detect_languages
    monkeypatch.setattr(manager, "detect_languages", lambda root: (walks.append(root), real(root))[1])
    Registry(project).get()
    assert walks == [project.root]


def test_home_root_skips_language_marker_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(manager.os, "walk", lambda *args, **kwargs: pytest.fail("home root was walked"))
    assert registry._language_markers(tmp_path, since=time.monotonic()) == ()
    assert manager.active_builtins(tmp_path) == ("fixes", "general", "graphite", "performance", "steering")


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


def test_a_rebuild_reuses_its_own_request_walk_and_refreshes_an_older_generation(
    project: CliState, clock: list[float], walks: list[Path]
) -> None:
    reg = Registry(project)
    reg.get()
    clock[0] += 1
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='edit-1'"))
    reg.get()
    assert walks == [project.root] * 2
    clock[0] += registry.MARKER_TTL
    (Path(project.hooks) / "h.py").write_text(HOOK.replace("message='m'", "message='edit-two'"))
    reg.get()
    assert walks == [project.root] * 3


@pytest.mark.parametrize("seeded", [False, True], ids=["cold", "seeded-entry-predates-the-build"])
def test_concurrent_cold_workers_share_one_language_walk(project: CliState, tmp_path: Path, seeded: bool) -> None:
    (nested := project.root / "services" / "api").mkdir(parents=True)
    if seeded:
        assert registry._language_markers(project.root, since=registry.ANY_AGE) == ()
    (nested / "go.mod").write_text("module x\n")
    (ledger := tmp_path / "ledger").touch()
    results: Queue[list[str]] = SPAWN.Queue()
    with registry.marker_lock(registry.marker_path(project.root)):
        procs = [spawn(cold_registry_languages, project.root, ledger, results) for _ in range(WORKERS)]
        await_ledger(ledger, "clock", WORKERS, procs)
    languages = [results.get(timeout=120) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)
    assert [proc.exitcode for proc in procs] == [0] * WORKERS
    assert len(ledger_pids(ledger, "walk")) == 1
    assert languages == [sorted(manager.detect_languages(project.root))] * WORKERS == [["go"]] * WORKERS


def test_shared_language_generation_expires_at_the_ttl_without_restamping(
    project: CliState, clock: list[float], walks: list[Path]
) -> None:
    registry._language_markers(project.root, since=registry.ANY_AGE)
    clock[0] = 1_000.0 + registry.MARKER_TTL - 0.001
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ()
    assert walks == [project.root]
    assert generation(project).computed_at == 1_000.0
    clock[0] = 1_000.0 + registry.MARKER_TTL
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = [pool.submit(registry._language_markers, project.root, since=registry.ANY_AGE) for _ in range(WORKERS)]
        served = [job.result() for job in jobs]
    assert served == [()] * WORKERS
    assert walks == [project.root] * 2
    assert generation(project).computed_at == 1_000.0 + registry.MARKER_TTL


def test_root_and_root_gitignore_stamps_invalidate_inside_the_ttl(
    project: CliState, clock: list[float], walks: list[Path]
) -> None:
    (services := project.root / "services").mkdir()
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ()
    (services / "go.mod").write_text("module x\n")
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ()
    (project.root / "README.md").write_text("readme\n")
    root = project.root.stat()
    os.utime(project.root, ns=(root.st_atime_ns, root.st_mtime_ns + 1_000_000_000))
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ("go",)
    (project.root / ".gitignore").write_text("*.log\nservices/\n")
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ()
    assert walks == [project.root] * 3


@pytest.mark.parametrize(
    ("marker", "ignores", "expected"),
    [
        pytest.param("services/api/go.mod", {}, ("go",), id="nested-go-mod"),
        pytest.param("tools/go.work", {}, ("go",), id="nested-go-work"),
        pytest.param("libs/core/pyproject.toml", {}, ("python",), id="nested-pyproject"),
        pytest.param("vendor/dep/go.mod", {".gitignore": "vendor/\n"}, (), id="ignored-directory"),
        pytest.param("svc/pyproject.toml", {"svc/.gitignore": "pyproject.toml\n"}, (), id="nested-ignore-file"),
        pytest.param("gen/pyproject.toml", {".gitignore": "/gen\n"}, (), id="anchored-ignore"),
        pytest.param("keep/go.mod", {".gitignore": "go.mod\n!keep/go.mod\n"}, ("go",), id="negated-ignore"),
        pytest.param(".jj/repo/go.mod", {}, (), id="vcs-metadata"),
    ],
)
def test_shared_generation_matches_the_detector_across_creation_and_deletion(
    project: CliState, clock: list[float], marker: str, ignores: dict[str, str], expected: tuple[str, ...]
) -> None:
    for rel, text in ignores.items():
        (ignore := project.root / rel).parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text(text)
    (path := project.root / marker).parent.mkdir(parents=True, exist_ok=True)
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ()
    path.write_text("x\n")
    clock[0] += registry.MARKER_TTL
    served = registry._language_markers(project.root, since=registry.ANY_AGE)
    assert served == tuple(sorted(manager.detect_languages(project.root))) == expected
    path.unlink()
    clock[0] += registry.MARKER_TTL
    served = registry._language_markers(project.root, since=registry.ANY_AGE)
    assert served == tuple(sorted(manager.detect_languages(project.root))) == ()


def test_roots_and_builds_keep_separate_generations(
    tmp_path: Path, clock: list[float], walks: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    go = make_project(tmp_path / "go").root
    py = make_project(tmp_path / "py").root
    (go / "go.mod").write_text("module x\n")
    (py / "pyproject.toml").write_text("[project]\n")
    assert registry._language_markers(go, since=registry.ANY_AGE) == ("go",)
    assert registry._language_markers(py, since=registry.ANY_AGE) == ("python",)
    assert registry._language_markers(go, since=registry.ANY_AGE) == ("go",)
    monkeypatch.setattr(registry, "marker_build", lambda: "another-build")
    assert registry._language_markers(go, since=registry.ANY_AGE) == ("go",)
    assert walks == [go, py, go]


def test_another_boot_never_reads_the_old_generation(
    project: CliState, clock: list[float], walks: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.root / "go.mod").write_text("module x\n")
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ("go",)
    old = registry.marker_path(project.root)
    monkeypatch.setattr(registry, "boot_id", lambda: "another-boot")
    assert registry._language_markers(project.root, since=registry.ANY_AGE) == ("go",)
    assert walks == [project.root] * 2
    assert registry.marker_path(project.root) == old.parent.parent / "another-boot" / old.name
    assert old.exists()


@pytest.mark.parametrize(
    ("platform", "error"),
    [
        pytest.param("linux", FileNotFoundError, id="unreadable-linux-boot-id"),
        pytest.param("freebsd14", RuntimeError, id="unsupported-platform"),
    ],
)
def test_an_unreadable_boot_id_fails_loudly(
    project: CliState,
    walks: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    error: type[Exception],
) -> None:
    monkeypatch.setattr(registry, "LINUX_BOOT_ID", tmp_path / "missing-boot-id")
    monkeypatch.setattr(registry.sys, "platform", platform)
    registry.boot_id.cache_clear()
    try:
        with pytest.raises(error):
            registry._language_markers(project.root, since=registry.ANY_AGE)
    finally:
        registry.boot_id.cache_clear()
    assert walks == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("{", id="truncated"),
        pytest.param(
            '{"stamp": [null, null], "computed_at": 1000.0, "languages": [], "owner": "x"}', id="unknown-field"
        ),
        pytest.param('{"stamp": [null, null], "computed_at": "1000", "languages": []}', id="string-clock"),
        pytest.param('{"stamp": [null, null], "computed_at": 1000.0}', id="missing-languages"),
        *(
            pytest.param(f'{{"stamp": [null, null], "computed_at": {clock}, "languages": []}}', id=f"clock-{clock}")
            for clock in ("Infinity", "-Infinity", "NaN", "1e999")
        ),
    ],
)
@pytest.mark.parametrize("fresh", [False, True], ids=["lookup", "build"])
def test_a_corrupt_shared_generation_fails_loudly(
    project: CliState, walks: list[Path], payload: str, fresh: bool
) -> None:
    (path := registry.marker_path(project.root)).parent.mkdir(parents=True)
    path.write_text(payload)
    with pytest.raises(ValidationError):
        registry._language_markers(project.root, since=time.monotonic() if fresh else registry.ANY_AGE)
    assert walks == []
    assert path.read_text() == payload


def test_concurrent_rewrites_never_expose_a_torn_generation(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    (project.root / "go.mod").write_text("module x\n")
    monkeypatch.setattr(registry, "MARKER_TTL", 0.0)
    start = threading.Barrier(WORKERS)

    def churn(_: int) -> set[tuple[str, ...]]:
        start.wait()
        return {registry._language_markers(project.root, since=registry.ANY_AGE) for _ in range(5)}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        assert set().union(*pool.map(churn, range(WORKERS))) == {("go",)}


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt], ids=["exception", "cancellation"])
def test_a_failed_walk_publishes_nothing_and_frees_the_lock(
    project: CliState, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    real = manager.detect_languages

    def interrupted(root: Path) -> frozenset[str]:
        raise error("walk interrupted")

    monkeypatch.setattr(manager, "detect_languages", interrupted)
    with pytest.raises(error):
        registry._language_markers(project.root, since=time.monotonic())
    path = registry.marker_path(project.root)
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []
    with registry.marker_lock(path).acquire(timeout=0):
        pass
    monkeypatch.setattr(manager, "detect_languages", real)
    assert registry._language_markers(project.root, since=time.monotonic()) == ()


def test_a_killed_walker_publishes_nothing_and_frees_the_lock(project: CliState, tmp_path: Path) -> None:
    (ledger := tmp_path / "ledger").touch()
    walker = spawn(stalled_walk, project.root, ledger)
    await_ledger(ledger, "walk", 1, [walker])
    walker.kill()
    walker.join(timeout=30)
    path = registry.marker_path(project.root)
    assert not path.exists()
    with registry.marker_lock(path).acquire(timeout=0):
        pass
    assert registry._language_markers(project.root, since=time.monotonic()) == ()


@pytest.mark.parametrize("stop", ["abandon", "deadline"])
def test_a_stopped_request_leaves_a_live_holders_lock_without_walking(
    project: CliState, tmp_path: Path, walks: list[Path], stop: str
) -> None:
    held = registry.marker_path(project.root)
    (ledger := tmp_path / "ledger").touch()
    holder = spawn(stalled_walk, project.root, ledger)
    await_ledger(ledger, "walk", 1, [holder])
    abandon = threading.Event()
    deadline_unix_ms = int((time.time() + 1.0) * 1000) if stop == "deadline" else 0
    overrides = reqenv.RequestOverrides(
        env=state_env(),
        cwd=str(project.root),
        client_ppid=1,
        session_id="s",
        deadline_unix_ms=deadline_unix_ms,
        abandon=abandon,
    )

    def wait() -> tuple[str, ...]:
        with reqenv.use_request(overrides):
            assert registry.marker_path(project.root) == held
            return registry._language_markers(project.root, since=time.monotonic())

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            waiter = pool.submit(wait)
            time.sleep(0.3)
            assert not waiter.done()
            if stop == "abandon":
                abandon.set()
            with pytest.raises(Timeout):
                waiter.result(timeout=30)
            assert holder.is_alive()
            assert walks == []
            assert not held.exists()
        finally:
            holder.kill()
            holder.join(timeout=30)


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


@pytest.mark.parametrize("policy", ["managed-settings.json", "managed-settings.d/policy.json"])
def test_unreadable_managed_settings_count_as_absent_until_readable(
    project: CliState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    plugin_root = tmp_path / "plug"
    make_plugin_pack(plugin_root)
    plant_roster([("acme/pp", plugin_root)])
    managed = tmp_path / "managed"
    (policy_file := managed / policy).parent.mkdir(parents=True)
    policy_file.write_text(json.dumps({"enabledPlugins": {"acme/pp": False}}))
    monkeypatch.setattr(plugins, "MANAGED_SETTINGS_DIRS", (managed,))
    policy_file.parent.chmod(0)
    try:
        unreadable = fp(project)
        assert [pid for pid, *_ in registry._plugin_trees(project.root)] == ["acme/pp"]
    finally:
        policy_file.parent.chmod(0o700)
    assert fp(project) != unreadable
    assert registry._plugin_trees(project.root) == ()


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

    def flaky_tree(hooks: str, *, fresh: bool = False) -> tuple[registry.HookEntry, ...]:
        if str(bad_root) in hooks:
            raise FileNotFoundError(hooks)
        return real(hooks, fresh=fresh)

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

    def racing_discover(self: CliState, *, languages: tuple[str, ...]) -> list[registry.manager.ResolvedPack]:
        calls.append(1)
        if len(calls) == 1:
            hook_file.write_text("")  # torn: discovery reads an empty, truncated hook file
            resolved = original(self, languages=languages)  # discovers nothing
            hook_file.write_text(HOOK)  # the rewrite completes right after the torn read
            return resolved
        return original(self, languages=languages)

    monkeypatch.setattr(CliState, "discover", racing_discover)
    snapshot = reg._build(time.monotonic())
    assert len(calls) == 2, "the build did not retry after the tree moved during discovery"
    assert any(h.spec.message == "m" for h in snapshot.state.hooks), (
        "the served snapshot is the torn/empty intermediate — the project hook (message='m') is missing"
    )


def test_build_is_bounded_when_the_tree_keeps_moving(
    project: CliState, monkeypatch: pytest.MonkeyPatch, walks: list[Path]
) -> None:
    # R4: an endlessly-churning tree must not spin forever — retries cap at BUILD_RETRIES.
    digests = iter(str(n) for n in range(1000))
    monkeypatch.setattr(
        Fingerprint, "with_languages", classmethod(lambda cls, cli, languages, *, fresh: cls(next(digests)))
    )
    reg = Registry(project)
    calls: list[int] = []
    real_discover = reg._discover_once
    monkeypatch.setattr(
        reg,
        "_discover_once",
        lambda fingerprint, languages: (calls.append(1), real_discover(fingerprint, languages))[1],
    )
    result = reg._build(time.monotonic())
    assert len(calls) == registry.BUILD_RETRIES, "the build was not bounded"
    assert walks == [project.root] * registry.BUILD_RETRIES
    assert result.state is not None
    assert result.cacheable is False, "an exhausted-retry (possibly torn) snapshot must not be cacheable"


def test_get_does_not_cache_a_non_cacheable_snapshot(project: CliState, monkeypatch: pytest.MonkeyPatch) -> None:
    # V9: a non-cacheable (exhausted/torn) snapshot is served but never stored — the next request rebuilds.
    from dataclasses import replace

    reg = Registry(project)
    builds: list[int] = []
    real = reg._discover_once
    monkeypatch.setattr(
        reg,
        "_discover_once",
        lambda fingerprint, languages: (builds.append(1), replace(real(fingerprint, languages), cacheable=False))[1],
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

    def counting(requested: float) -> registry.RegistrySnapshot:
        nonlocal builds
        with lock:
            builds += 1
        time.sleep(0.05)  # widen the window so peers pile up on the build lock
        return original(requested)

    reg._build = counting
    start = threading.Barrier(12)

    def worker() -> registry.RegistrySnapshot:
        start.wait()
        return reg.get()

    with ThreadPoolExecutor(max_workers=12) as pool:
        snaps = [f.result() for f in [pool.submit(worker) for _ in range(12)]]

    assert builds == 1
    assert all(s is snaps[0] for s in snaps)


def test_a_persistently_unreadable_roster_is_served_as_service_time_not_warm_up(
    project: CliState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(root: Path) -> list[manager.ResolvedPack]:
        raise plugins.PluginListError("plugin roster unreadable")

    monkeypatch.setattr(plugins, "resolve_plugin_packs", unreadable)
    reg = Registry(project)
    requests = [
        reqenv.RequestOverrides(env=state_env(), cwd=str(project.root), client_ppid=1, session_id="s") for _ in range(3)
    ]
    for overrides in requests:
        with reqenv.use_request(overrides):
            assert not reg.get().cacheable

    assert [overrides.warmups for overrides in requests] == [[], [], []]


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
