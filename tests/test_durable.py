from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from captain_hook import Deque, DurableSlot, DurableState, DurableStore


class Shapes(DurableState, scope="project"):
    items: Deque[256]


class GlobalShapes(DurableState, scope="global"):
    items: Deque[256]


def fake_evt(repo_root: Path | None) -> SimpleNamespace:
    return SimpleNamespace(ctx=SimpleNamespace(repo_root=repo_root))


class TestDurableStoreMechanics:
    def test_getitem_returns_durable_slot(self, tmp_path: Path) -> None:
        assert isinstance(DurableStore(tmp_path)[Shapes], DurableSlot)

    def test_set_get_roundtrip(self, tmp_path: Path) -> None:
        store = DurableStore(tmp_path)
        m = Shapes()
        m.items.append("x")
        store[Shapes].set(m)
        assert list(store[Shapes].get(Shapes()).items) == ["x"]

    def test_snake_case_filename(self, tmp_path: Path) -> None:
        path = DurableStore(tmp_path)[GlobalShapes].path
        assert path is not None
        assert path.name == "global_shapes.json"

    def test_corrupt_json_returns_default(self, tmp_path: Path) -> None:
        (tmp_path / "shapes.json").write_text("{not json")
        assert list(DurableStore(tmp_path).load(Shapes).items) == []

    def test_none_dir_noop(self) -> None:
        store = DurableStore(None)
        store[Shapes].set(Shapes())
        assert store[Shapes].get() is None
        with store[Shapes].mutate() as m:
            m.items.append("ephemeral")
        assert store[Shapes].get() is None


class TestDurableScope:
    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(tmp_path))
        self.tmp = tmp_path

    def test_cross_session_persistence(self) -> None:
        evt = fake_evt(Path("/repo/a"))
        with Shapes.mutate(evt) as m:
            m.items.append("gh --json")
        assert list(Shapes.load(evt).items) == ["gh --json"]

    def test_project_isolation(self) -> None:
        a, b = fake_evt(Path("/repo/a")), fake_evt(Path("/repo/b"))
        with Shapes.mutate(a) as m:
            m.items.append("only-a")
        assert list(Shapes.load(a).items) == ["only-a"]
        assert list(Shapes.load(b).items) == []

    def test_global_shared_across_repos(self) -> None:
        a, b = fake_evt(Path("/repo/a")), fake_evt(Path("/repo/b"))
        with GlobalShapes.mutate(a) as m:
            m.items.append("shared")
        assert list(GlobalShapes.load(b).items) == ["shared"]

    def test_project_none_repo_root_persists_nothing(self) -> None:
        evt = fake_evt(None)
        with Shapes.mutate(evt) as m:
            m.items.append("ephemeral")
        assert list(Shapes.load(evt).items) == []

    def test_scope_kwarg_writes_to_global_path(self) -> None:
        with GlobalShapes.mutate(fake_evt(Path("/repo/a"))) as m:
            m.items.append("g")
        assert (self.tmp / "hooks" / "durable" / "global" / "global_shapes.json").exists()

    def test_bad_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="project"):

            class Bad(DurableState, scope="nope"):
                pass

    def test_bounded_deque_survives_reload(self) -> None:
        evt = fake_evt(Path("/repo/cap"))

        class Tiny(DurableState, scope="project"):
            items: Deque[3]

        with Tiny.mutate(evt) as m:
            for i in range(10):
                m.items.append(str(i))
        reloaded = Tiny.load(evt)
        assert list(reloaded.items) == ["7", "8", "9"]
        assert reloaded.items.maxlen == 3

    def test_concurrent_mutate_no_lost_update(self) -> None:
        evt = fake_evt(Path("/repo/conc"))

        def worker(i: int) -> None:
            with Shapes.mutate(evt) as m:
                time.sleep(0.005)
                m.items.append(f"k{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(Shapes.load(evt).items) == {f"k{i}" for i in range(20)}

    def test_plain_set_loses_update_without_lock(self) -> None:
        evt = fake_evt(Path("/repo/race"))
        store = DurableStore.for_event(evt, scope="project")
        a = store[Shapes].get(Shapes())
        a.items.append("a")
        b = store[Shapes].get(Shapes())
        b.items.append("b")
        store[Shapes].set(a)
        store[Shapes].set(b)
        assert list(Shapes.load(evt).items) == ["b"]
