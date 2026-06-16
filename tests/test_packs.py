from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import captain_hook
from captain_hook import app
from captain_hook.cli import cli
from captain_hook.loader import discover_pack
from captain_hook.packs import manager

PACKS_DIR = Path(captain_hook.__file__).parent / "packs"
EXPECTED_BUILTINS = {"general", "python"}
GENERAL_HOOKS = {"commands", "docs", "plans", "prompts", "review", "stewardship", "tasks"}
PYTHON_HOOKS = {"style", "testing", "toolchain"}
HOOK_SRC = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"


def write_pack(root: Path, name: str, *, hooks: str = ".", version: str = "0.1.0") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / manager.PACK_MANIFEST).write_text(
        f'name = "{name}"\nversion = "{version}"\ndescription = "d"\nhooks = "{hooks}"\n'
    )
    return root


def make_pack_tarball(dest: Path, *, name: str, top: str) -> Path:
    src = dest / "src"
    write_pack(src, name, hooks="hooks")
    (src / "hooks").mkdir()
    (src / "hooks" / "h.py").write_text(HOOK_SRC)
    tarball = dest / f"{name}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(src, arcname=top)
    return tarball


# --- builtin pack content -------------------------------------------------------------


def test_expected_builtin_packs_present() -> None:
    on_disk = {p.name for p in PACKS_DIR.iterdir() if p.is_dir() and (p / manager.PACK_MANIFEST).is_file()}
    assert on_disk == EXPECTED_BUILTINS
    assert set(manager.builtin_packs()) == EXPECTED_BUILTINS


@pytest.mark.parametrize(
    ("name", "hook_stems"),
    [("general", GENERAL_HOOKS), ("python", PYTHON_HOOKS)],
    ids=["general", "python"],
)
def test_builtin_pack_layout(name: str, hook_stems: set[str]) -> None:
    pack_dir = manager.builtin_packs()[name]
    manifest = manager.PackManifest.load(pack_dir / manager.PACK_MANIFEST)
    assert manifest.name == name
    assert {p.stem for p in pack_dir.glob("*.py") if not p.stem.startswith("_")} == hook_stems


# --- PackSource ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "owner", "repo", "ref"),
    [
        ("github:acme/guards", "acme", "guards", None),
        ("github:acme/guards@v1.2", "acme", "guards", "v1.2"),
        ("github:a-b/c.d@feat/x", "a-b", "c.d", "feat/x"),
    ],
)
def test_pack_source_round_trip(raw: str, owner: str, repo: str, ref: str | None) -> None:
    source = manager.PackSource.parse(raw)
    assert (source.owner, source.repo, source.ref) == (owner, repo, ref)
    assert str(source) == raw


@pytest.mark.parametrize("raw", ["acme/guards", "github:acme", "gitlab:a/b", "github:a/b@"])
def test_pack_source_rejects_invalid(raw: str) -> None:
    with pytest.raises(manager.PackError):
        manager.PackSource.parse(raw)


# --- PackManifest --------------------------------------------------------------------


def test_manifest_load_and_hooks_dir(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p", "acme-guards", hooks="hooks")
    manifest = manager.PackManifest.load(pack / manager.PACK_MANIFEST)
    assert (manifest.name, manifest.version, manifest.hooks) == ("acme-guards", "0.1.0", "hooks")
    assert manifest.hooks_dir(pack) == pack / "hooks"


def test_manifest_rejects_bad_name(tmp_path: Path) -> None:
    (tmp_path / manager.PACK_MANIFEST).write_text('name = "Bad_Name"\nversion = "0"\ndescription = "d"\nhooks = "."\n')
    with pytest.raises(manager.PackError):
        manager.PackManifest.load(tmp_path / manager.PACK_MANIFEST)


def test_manifest_missing_field_fails_loud(tmp_path: Path) -> None:
    (tmp_path / manager.PACK_MANIFEST).write_text('name = "x"\nversion = "0"\nhooks = "."\n')
    with pytest.raises(KeyError):
        manager.PackManifest.load(tmp_path / manager.PACK_MANIFEST)


def test_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(manager.PackError):
        manager.PackManifest.load(tmp_path / manager.PACK_MANIFEST)


# --- packs.toml IO -------------------------------------------------------------------


def test_packs_toml_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "packs.toml"
    builtin = manager.BuiltinPack(name="general")
    external = manager.ExternalPack(name="acme", source=manager.PackSource.parse("github:a/b@v1"), commit="a" * 40)
    manager.atomic_write(path, manager.render_packs_toml([external, builtin]))
    assert manager.read_entries(path) == [external, builtin]  # rendered sorted by name: "acme" < "general"


def test_upsert_replaces_same_name(tmp_path: Path) -> None:
    path = tmp_path / "packs.toml"
    manager.upsert_entry(path, manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v1"), "a" * 40))
    manager.upsert_entry(path, manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v2"), "b" * 40))
    (entry,) = manager.read_entries(path)
    assert isinstance(entry, manager.ExternalPack) and entry.commit == "b" * 40


def test_delete_entry(tmp_path: Path) -> None:
    path = tmp_path / "packs.toml"
    manager.upsert_entry(path, manager.BuiltinPack("general"))
    manager.upsert_entry(path, manager.BuiltinPack("python"))
    manager.delete_entry(path, "general")
    assert manager.read_entries(path) == [manager.BuiltinPack("python")]
    with pytest.raises(manager.PackError):
        manager.delete_entry(path, "missing")


def test_read_entries_rejects_partial_external(tmp_path: Path) -> None:
    path = tmp_path / "packs.toml"
    path.write_text('[packs.acme]\nsource = "github:a/b@v1"\n')
    with pytest.raises(manager.PackError):
        manager.read_entries(path)


# --- fetch / cache -------------------------------------------------------------------


def test_strip_top_level(tmp_path: Path) -> None:
    tarball = make_pack_tarball(tmp_path, name="x", top="repo-sha")
    with tarfile.open(tarball) as tf:
        names = {m.path for m in manager.strip_top_level(tf)}
    assert {"capt-hook.toml", "hooks/h.py"} <= names
    assert not any(n.startswith("repo-sha") for n in names)


def test_fetch_pack_caches_and_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    tarball = make_pack_tarball(tmp_path, name="acme-guards", top="guards-abc")
    sha = "d" * 40
    monkeypatch.setattr(manager, "resolve_commit", lambda source: sha)
    monkeypatch.setattr("urllib.request.urlretrieve", lambda url: (str(tarball), None))

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/guards@v1"))

    assert resolved.entry == manager.ExternalPack("acme-guards", manager.PackSource.parse("github:acme/guards@v1"), sha)
    assert resolved.manifest.name == "acme-guards"
    assert (resolved.path / "h.py").is_file()
    assert manager.find_cached("acme-guards", sha) == tmp_path / "cache" / f"acme-guards@{sha}"
    assert manager.find_cached("acme-guards", "e" * 40) is None


def test_fetch_pack_caches_only_manifest_and_hooks_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    repo = tmp_path / "repo"
    write_pack(repo, "acme-guards", hooks="plugin/hooks")
    (repo / "plugin" / "hooks").mkdir(parents=True)
    (repo / "plugin" / "hooks" / "h.py").write_text(HOOK_SRC)
    (repo / "README.md").write_text("# big repo\n")
    (repo / "go.mod").write_text("module example.com/big\n")
    (repo / "src").mkdir()
    (repo / "src" / "big.txt").write_text("x" * 4096)
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="big-repo-sha")

    sha = "d" * 40
    monkeypatch.setattr(manager, "resolve_commit", lambda source: sha)
    monkeypatch.setattr("urllib.request.urlretrieve", lambda url: (str(tarball), None))

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/guards@v1"))

    cached = tmp_path / "cache" / f"acme-guards@{sha}"
    assert {p.name for p in cached.iterdir()} == {manager.PACK_MANIFEST, "plugin", manager.SHA_MARKER}
    assert (cached / "plugin" / "hooks" / "h.py").is_file()
    assert (resolved.path / "h.py").is_file()
    for junk in ("README.md", "go.mod", "src"):
        assert not (cached / junk).exists(), f"{junk} leaked into the cache"


def test_fetch_pack_root_hooks_caches_full_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    repo = tmp_path / "repo"
    write_pack(repo, "acme-flat", hooks=".")
    (repo / "h.py").write_text(HOOK_SRC)
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="flat-sha")

    sha = "e" * 40
    monkeypatch.setattr(manager, "resolve_commit", lambda source: sha)
    monkeypatch.setattr("urllib.request.urlretrieve", lambda url: (str(tarball), None))

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/flat@v1"))

    cached = tmp_path / "cache" / f"acme-flat@{sha}"
    assert (cached / manager.PACK_MANIFEST).is_file()
    assert (cached / "h.py").is_file()
    assert (resolved.path / "h.py").is_file()


def test_fetch_pack_missing_manifest_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# no manifest here\n")
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="no-manifest-sha")

    monkeypatch.setattr(manager, "resolve_commit", lambda source: "f" * 40)
    monkeypatch.setattr("urllib.request.urlretrieve", lambda url: (str(tarball), None))

    with pytest.raises(manager.PackError):
        manager.fetch_pack(manager.PackSource.parse("github:acme/empty@v1"))


# --- resolution ----------------------------------------------------------------------


def test_resolve_enabled_builtin(tmp_path: Path) -> None:
    manager.upsert_entry(manager.packs_toml_path(tmp_path), manager.BuiltinPack("general"))
    resolved, missing = manager.resolve_enabled_packs(tmp_path)
    assert missing == []
    (general,) = resolved
    assert general.entry == manager.BuiltinPack("general")
    assert general.manifest.name == "general"


def test_resolve_unknown_builtin_fails_loud(tmp_path: Path) -> None:
    manager.upsert_entry(manager.packs_toml_path(tmp_path), manager.BuiltinPack("nope"))
    with pytest.raises(manager.PackError):
        manager.resolve_enabled_packs(tmp_path)


def test_resolve_uncached_external_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v1"), "a" * 40),
    )
    resolved, missing = manager.resolve_enabled_packs(tmp_path)
    assert (resolved, missing) == ([], ["acme"])


# --- loader.discover_pack ------------------------------------------------------------


def test_discover_pack_skips_underscore_and_conf(tmp_path: Path) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    for stem in ("hook", "_skip", "conf"):
        (pack / f"{stem}.py").write_text(HOOK_SRC)
    discover_pack("solo", pack)
    assert len(app._state.hooks) == 1
    assert "captain_hook._packs.solo.hook" in sys.modules


def test_discover_pack_namespaces_avoid_collision(tmp_path: Path) -> None:
    for name in ("a", "b"):
        pack = tmp_path / name
        pack.mkdir()
        (pack / "commands.py").write_text(HOOK_SRC)
        discover_pack(name, pack)
    assert len(app._state.hooks) == 2
    assert "captain_hook._packs.a.commands" in sys.modules
    assert "captain_hook._packs.b.commands" in sys.modules


def test_discover_pack_sanitizes_name(tmp_path: Path) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "h.py").write_text(HOOK_SRC)
    discover_pack("acme-guards@d4", pack)
    assert "captain_hook._packs.acme_guards_d4.h" in sys.modules


# --- CLI -----------------------------------------------------------------------------


def test_cli_pack_add_list_remove(tmp_path: Path) -> None:
    runner = CliRunner()
    add = runner.invoke(cli, ["--root", str(tmp_path), "pack", "add", "general"])
    assert add.exit_code == 0, add.output
    assert "[packs.general]" in manager.packs_toml_path(tmp_path).read_text()

    listed = runner.invoke(cli, ["--root", str(tmp_path), "pack", "list"])
    assert "general" in listed.output and "7 hooks" in listed.output

    remove = runner.invoke(cli, ["--root", str(tmp_path), "pack", "remove", "general"])
    assert remove.exit_code == 0
    assert manager.read_entries(manager.packs_toml_path(tmp_path)) == []


def test_cli_pack_add_rejects_invalid_target(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "add", "not-a-pack"])
    assert result.exit_code != 0
    assert not manager.packs_toml_path(tmp_path).exists()
