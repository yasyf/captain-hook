from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from captain_hook.packs import plugins
from tests.test_plugin_packs import install, install_record, write_plugin_pack


@pytest.fixture
def resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    if before := os.environ.get("CAPT_PLUGIN_RESOLUTION_BASELINE"):
        spec = importlib.util.spec_from_file_location("plugin_resolution_before", before)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = plugins
    monkeypatch.setattr(module, "MANAGED_SETTINGS_DIRS", ())
    try:
        yield module
    finally:
        if before:
            sys.modules.pop(module.__name__)


@pytest.mark.parametrize("size", [32, 4096])
def test_repeated_plugin_paths_are_probed_once_per_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolver: ModuleType, size: int
) -> None:
    (root := tmp_path / "project").mkdir()
    (alias := tmp_path / "project-alias").symlink_to(root, target_is_directory=True)
    plugin = write_plugin_pack(tmp_path, "probe")
    record = install_record(plugin, scope="project", project_path=alias)
    install({"probe@probe": [dict(record) for _ in range(size)]})
    counts = {"directory": 0, "canonical": 0}
    original_directory, original_resolve = Path.is_dir, Path.resolve

    def directory(path: Path, *, follow_symlinks: bool = True) -> bool:
        counts["directory"] += path == plugin
        return original_directory(path, follow_symlinks=follow_symlinks)

    def canonical(path: Path, strict: bool = False) -> Path:
        counts["canonical"] += path == alias
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "is_dir", directory)
    monkeypatch.setattr(Path, "resolve", canonical)
    selected = resolver.enabled_plugins(root)
    assert [(item.id, item.root, item.scope, item.project_path) for item in selected] == [
        ("probe@probe", str(plugin), "project", str(alias))
    ]
    print(json.dumps({"records": size} | counts))
    assert counts == {"directory": 1, "canonical": 1}, "duplicate plugin filesystem probes were repeated"


def test_resolution_memo_spans_ids_and_expires_before_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolver: ModuleType
) -> None:
    (root := tmp_path / "project").mkdir()
    (other := tmp_path / "other").mkdir()
    (alias := tmp_path / "project-alias").symlink_to(root, target_is_directory=True)
    plugin = write_plugin_pack(tmp_path, "probe")
    record = install_record(plugin, scope="project", project_path=alias)
    install({"a@probe": [record], "b@probe": [record]})
    operations = []
    original_directory, original_resolve = Path.is_dir, Path.resolve

    def directory(path: Path, *, follow_symlinks: bool = True) -> bool:
        if path == plugin:
            operations.append("directory")
        return original_directory(path, follow_symlinks=follow_symlinks)

    def canonical(path: Path, strict: bool = False) -> Path:
        if path == alias:
            operations.append("canonical")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "is_dir", directory)
    monkeypatch.setattr(Path, "resolve", canonical)
    assert [item.id for item in resolver.enabled_plugins(root)] == ["a@probe", "b@probe"]
    assert operations == ["directory", "canonical"]
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    assert resolver.enabled_plugins(root) == ()
    assert operations == ["directory", "canonical"] * 2
    shutil.rmtree(plugin)
    assert resolver.enabled_plugins(root) == ()
    assert operations == ["directory", "canonical"] * 2 + ["directory"]


@pytest.mark.parametrize("kind", ["directory", "canonical"])
def test_resolution_probe_errors_propagate_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolver: ModuleType, kind: str
) -> None:
    (root := tmp_path / "project").mkdir()
    (alias := tmp_path / "project-alias").symlink_to(root, target_is_directory=True)
    plugin = write_plugin_pack(tmp_path, "probe")
    install({"probe@probe": [install_record(plugin, scope="project", project_path=alias)] * 2})
    original_directory, original_resolve = Path.is_dir, Path.resolve
    failure = PermissionError("owned resolution probe failure")

    def directory(path: Path, *, follow_symlinks: bool = True) -> bool:
        if path == plugin:
            raise failure
        return original_directory(path, follow_symlinks=follow_symlinks)

    def canonical(path: Path, strict: bool = False) -> Path:
        if path == alias:
            raise failure
        return original_resolve(path, strict=strict)

    if kind == "directory":
        monkeypatch.setattr(Path, "is_dir", directory)
    else:
        monkeypatch.setattr(Path, "resolve", canonical)
    with pytest.raises(PermissionError) as caught:
        resolver.enabled_plugins(root)
    assert caught.value is failure
