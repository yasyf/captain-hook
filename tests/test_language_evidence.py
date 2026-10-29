from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from captain_hook.packs import manager


def complete_markers(root: Path) -> tuple[Path, Path]:
    paths = (root / "services/api/go.mod", root / "packages/lib/pyproject.toml")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    return paths


class NoIteration:
    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        raise AssertionError("positive witness renewal enumerated directory entries")


def test_complete_proof_renews_without_enumerating_wide_witness_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    go, _ = complete_markers(tmp_path)
    for number in range(512):
        (go.parent / f"other-{number}").touch()
    evidence = manager.language_evidence(tmp_path)
    assert evidence.languages == ("go", "python")
    assert evidence.directories
    opened: list[Path] = []
    real = manager.os.scandir

    @contextmanager
    def metadata_only(path: Path) -> Iterator[NoIteration]:
        with real(path):
            opened.append(path)
            yield NoIteration()

    monkeypatch.setattr(manager.os, "scandir", metadata_only)
    assert evidence.valid(tmp_path)
    assert opened == [
        tmp_path / relative for relative in sorted(dict(evidence.directories), key=lambda name: len(Path(name).parts))
    ]


@pytest.mark.parametrize("change", ["remove", "directory", "go-work", "marker-case", "ancestor-case", "symlink-parent"])
def test_marker_and_ancestry_changes_invalidate_positive_proof(tmp_path: Path, change: str) -> None:
    go, _ = complete_markers(tmp_path)
    evidence = manager.language_evidence(tmp_path)
    match change:
        case "remove":
            go.unlink()
        case "directory":
            go.unlink()
            go.mkdir()
        case "go-work":
            go.rename(go.with_name("go.work"))
        case "marker-case":
            go.rename(go.with_name("GO.MOD"))
        case "ancestor-case":
            (tmp_path / "services").rename(tmp_path / "SERVICES")
        case "symlink-parent":
            (tmp_path / "services").rename(tmp_path / "moved-services")
            (tmp_path / "services").symlink_to(tmp_path / "moved-services", target_is_directory=True)
    assert not evidence.valid(tmp_path)
    regenerated = manager.language_evidence(tmp_path)
    assert regenerated.languages == tuple(sorted(manager.detect_languages(tmp_path)))


@pytest.mark.parametrize("ignore", [".gitignore", "services/.gitignore", "services/api/.gitignore"])
@pytest.mark.parametrize("existing", [False, True])
def test_current_ancestor_ignore_rules_invalidate_witnesses(tmp_path: Path, ignore: str, existing: bool) -> None:
    complete_markers(tmp_path)
    path = tmp_path / ignore
    if existing:
        path.write_text("")
    evidence = manager.language_evidence(tmp_path)
    path.write_text("go.mod\n")
    assert not evidence.valid(tmp_path)
    assert manager.language_evidence(tmp_path).languages == ("python",)


def test_ignored_ancestor_prunes_before_its_own_negation(tmp_path: Path) -> None:
    complete_markers(tmp_path)
    (tmp_path / ".gitignore").write_text("")
    (tmp_path / "services/api/.gitignore").write_text("!go.mod\n")
    evidence = manager.language_evidence(tmp_path)
    (tmp_path / ".gitignore").write_text("services/\n")
    assert not evidence.valid(tmp_path)
    assert manager.language_evidence(tmp_path).languages == ("python",)


def test_unreachable_old_ignore_error_does_not_replace_authoritative_short_circuit(tmp_path: Path) -> None:
    complete_markers(tmp_path)
    (tmp_path / "go.mod").touch()
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / ".gitignore").write_text("/go.mod\n/pyproject.toml\n")
    old_ignore = tmp_path / "services/api/.gitignore"
    old_ignore.write_text("")
    evidence = manager.language_evidence(tmp_path)
    assert dict(evidence.witnesses)["go"] == "services/api/go.mod"
    (tmp_path / ".gitignore").write_text("")
    old_ignore.write_bytes(b"\xff")
    assert not evidence.valid(tmp_path)
    assert manager.language_evidence(tmp_path).languages == ("go", "python")


def test_authoritative_discovery_still_raises_reachable_ignore_errors(tmp_path: Path) -> None:
    complete_markers(tmp_path)
    ignore = tmp_path / "services/api/.gitignore"
    ignore.write_text("")
    evidence = manager.language_evidence(tmp_path)
    ignore.write_bytes(b"\xff")
    assert not evidence.valid(tmp_path)
    with pytest.raises(UnicodeDecodeError):
        manager.language_evidence(tmp_path)


def test_stale_witness_pattern_error_is_recomputed_from_authoritative_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete_markers(tmp_path)
    (tmp_path / "go.mod").touch()
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / ".gitignore").write_text("/go.mod\n/pyproject.toml\n")
    evidence = manager.language_evidence(tmp_path)
    (tmp_path / ".gitignore").write_text("")
    real = manager.descend_spec

    def invalid_old_pattern(
        parent: manager.GitIgnoreSpec | None, own: list[str], relative: str
    ) -> manager.GitIgnoreSpec | None:
        if relative == "services/api":
            raise ValueError("invalid old-path gitignore pattern")
        return real(parent, own, relative)

    monkeypatch.setattr(manager, "descend_spec", invalid_old_pattern)
    assert not evidence.valid(tmp_path)
    assert manager.language_evidence(tmp_path).languages == ("go", "python")


def test_unrelated_programming_value_error_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    complete_markers(tmp_path)
    evidence = manager.language_evidence(tmp_path)

    def programming_error(path: Path, *, root: bool) -> manager.DirectoryStamp:
        raise ValueError("unrelated programming error")

    monkeypatch.setattr(manager, "directory_stamp", programming_error)
    with pytest.raises(ValueError, match="unrelated programming error"):
        evidence.valid(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read execute-only directories")
def test_execute_only_ancestor_is_not_a_valid_walk_witness(tmp_path: Path) -> None:
    go, _ = complete_markers(tmp_path)
    evidence = manager.language_evidence(tmp_path)
    go.parent.chmod(0o111)
    try:
        assert go.lstat()
        assert not evidence.valid(tmp_path)
        assert manager.detect_languages(tmp_path) == frozenset({"python"})
    finally:
        go.parent.chmod(0o755)


def test_root_symlink_is_followed_but_descendant_symlink_directory_is_not(tmp_path: Path) -> None:
    real = tmp_path / "real"
    complete_markers(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    evidence = manager.language_evidence(alias)
    assert evidence.languages == ("go", "python")
    assert evidence.valid(alias)


def test_dangling_and_special_markers_count_but_directory_markers_do_not(tmp_path: Path) -> None:
    (tmp_path / "go.mod").symlink_to(tmp_path / "missing")
    os.mkfifo(tmp_path / "pyproject.toml")
    evidence = manager.language_evidence(tmp_path)
    assert evidence.languages == ("go", "python")
    assert evidence.valid(tmp_path)
    (tmp_path / "missing").mkdir()
    assert not evidence.valid(tmp_path)
    assert manager.detect_languages(tmp_path) == frozenset({"python"})


@pytest.mark.parametrize("rename", ["marker", "ancestor"])
def test_rename_between_discovery_and_certification_never_creates_a_positive_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rename: str
) -> None:
    go, _ = complete_markers(tmp_path)
    real = manager._language_witnesses
    changed = False

    def moved(root: Path) -> dict[str, str]:
        nonlocal changed
        result = real(root)
        if not changed:
            changed = True
            if rename == "marker":
                go.rename(go.with_name("GO.MOD"))
            else:
                (tmp_path / "services").rename(tmp_path / "SERVICES")
        return result

    monkeypatch.setattr(manager, "_language_witnesses", moved)
    evidence = manager.language_evidence(tmp_path)
    assert evidence.languages == ("go", "python")
    assert evidence.directories == ()
    assert not evidence.valid(tmp_path)


def test_new_supported_language_invalidates_a_previous_complete_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete_markers(tmp_path)
    evidence = manager.language_evidence(tmp_path)
    monkeypatch.setitem(manager.LANGUAGE_MARKERS, "rust", ("Cargo.toml",))
    (tmp_path / "Cargo.toml").touch()
    assert not evidence.valid(tmp_path)
    regenerated = manager.language_evidence(tmp_path)
    assert regenerated.languages == ("go", "python", "rust")
    assert regenerated.valid(tmp_path)
