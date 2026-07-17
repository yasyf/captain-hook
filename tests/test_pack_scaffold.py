from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click
import pytest

from captain_hook.packs import manager, scaffold
from tests.helpers import run_cli

pytestmark = pytest.mark.usefixtures("isolate_modules")

# A discovery-era pack plugin ships three artifacts plus its hooks — no hooks.json.
ARTIFACTS = {"capt-hook.toml", "plugin.json", "marketplace.json", "guard.py"}
CAPTAIN_DEP = [{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=9.8.0"}]
POST_TOOL_HOOK = 'from captain_hook import Event, hook\n\nhook(Event.PostToolUse, message="m")\n'


# --- fixture builders ----------------------------------------------------------------


def write_manifest(root: Path, *, name: str = "ccx", hooks: str = ".", marketplaces: list[str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    body = f'[pack]\nname = "{name}"\nversion = "0.1.0"\ndescription = "d"\nhooks = "{hooks}"\n'
    if marketplaces is not None:
        body += f"marketplaces = {json.dumps(marketplaces)}\n"
    (root / manager.PACK_MANIFEST).write_text(body)


def write_hook(root: Path) -> None:
    (root / "h.py").write_text(POST_TOOL_HOOK)


def write_plugin_json(root: Path, deps: list[object]) -> None:
    (d := root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"name": "ccx", "dependencies": deps}))


def write_marketplace(root: Path, allow: list[str]) -> None:
    (d := root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / "marketplace.json").write_text(json.dumps({"name": "ccx", "allowCrossMarketplaceDependenciesOn": allow}))


def conforming(root: Path) -> Path:
    """A pack that already satisfies the discovery contract's three artifacts (no hooks.json)."""
    write_manifest(root)
    write_hook(root)
    write_plugin_json(root, CAPTAIN_DEP)
    write_marketplace(root, ["captain-hook"])
    return root


# --- helpers -------------------------------------------------------------------------


def file_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def verbs(actions: list[scaffold.ScaffoldAction]) -> list[str]:
    return [a.verb for a in actions]


def action_for(actions: list[scaffold.ScaffoldAction], name: str) -> scaffold.ScaffoldAction:
    return next(a for a in actions if a.path.name == name)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def no_hooks_json(root: Path) -> bool:
    return not any(p.name == "hooks.json" for p in root.rglob("*"))


# --- greenfield: create every discovery-contract artifact, no hooks.json --------------


def test_empty_dir_scaffolds_every_artifact(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert verbs(actions) == ["created"] * 4
    assert {a.path.name for a in actions} == ARTIFACTS
    assert no_hooks_json(root)  # a discovery-era pack ships zero capt-hook invocations


def test_no_hooks_json_planned_or_written(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert not any(a.path.name == "hooks.json" for a in actions)
    assert no_hooks_json(root)


def test_missing_manifest_written_as_pack_grammar(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    scaffold.scaffold_pack(root, name="myfilter", description="My filter pack")
    text = manager.manifest_in(root).read_text()
    assert text.startswith("[pack]")  # the [pack] table header the new grammar requires
    manifest = manager.PackManifest.load(manager.manifest_in(root))
    assert (manifest.name, manifest.description, manifest.hooks) == ("myfilter", "My filter pack", "hooks")


# --- idempotence + conforming packs stay byte-identical ------------------------------


def test_second_run_is_idempotent_and_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    scaffold.scaffold_pack(root, name="pkg", description="d")
    before = file_hashes(root)
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert all(a.verb == "unchanged" for a in actions)
    assert file_hashes(root) == before


def test_conforming_pack_is_left_untouched(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    before = file_hashes(root)
    actions = scaffold.scaffold_pack(root, name="ccx", description="d")
    assert all(a.verb == "unchanged" for a in actions)
    assert not any(a.path.name == "guard.py" for a in actions)  # the existing hook is not displaced
    assert file_hashes(root) == before


def test_existing_valid_manifest_left_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "ccx"
    write_manifest(root, name="ccx")
    before = (root / manager.PACK_MANIFEST).read_bytes()
    manifest_action = scaffold.scaffold_pack(root, name="ccx", description="d")[0]
    assert manifest_action.verb == "unchanged"
    assert (root / manager.PACK_MANIFEST).read_bytes() == before


# --- plugin.json / marketplace.json surgical upsert (dep/allowlist unchanged) ---------


def test_conforming_version_floor_is_never_bumped(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")  # ships a ">=9.8.0" floor, older than the scaffold default
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "plugin.json").verb == "unchanged"
    dep = read_json(root / ".claude-plugin" / "plugin.json")["dependencies"][0]
    assert dep["version"] == ">=9.8.0"


def test_partial_dependency_gains_only_missing_fields(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook"}, {"name": "other-plugin"}])
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "plugin.json").verb == "updated"
    deps = read_json(root / ".claude-plugin" / "plugin.json")["dependencies"]
    captain = next(d for d in deps if d.get("name") == "captain-hook")
    assert captain == {"name": "captain-hook", "marketplace": "captain-hook", "version": scaffold.dependency_floor()}
    assert {"name": "other-plugin"} in deps  # unrelated dependency preserved verbatim


def test_marketplace_allowlist_gains_captain_hook_and_leaves_owner(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "ccx", "owner": {"name": "Someone"}, "allowCrossMarketplaceDependenciesOn": ["other"]})
    )
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "marketplace.json").verb == "updated"
    data = read_json(root / ".claude-plugin" / "marketplace.json")
    assert data["allowCrossMarketplaceDependenciesOn"] == ["other", "captain-hook"]
    assert data["owner"] == {"name": "Someone"}


def test_foreign_dep_claiming_marketplace_is_left_untouched(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "other-plugin", "marketplace": "captain-hook"}])
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "plugin.json").verb == "updated"
    deps = read_json(root / ".claude-plugin" / "plugin.json")["dependencies"]
    assert {"name": "other-plugin", "marketplace": "captain-hook"} in deps  # the foreign dep is preserved
    captain = next(d for d in deps if d.get("name") == "captain-hook")
    assert captain == {"name": "captain-hook", "marketplace": "captain-hook", "version": scaffold.dependency_floor()}


# --- starter hook + version floor ----------------------------------------------------


def test_starter_hook_only_seeded_when_hooks_dir_empty(tmp_path: Path) -> None:
    with_hook = scaffold.scaffold_pack(conforming(tmp_path / "ccx"), name="ccx", description="d")
    assert not any(a.path.name == "guard.py" for a in with_hook)
    greenfield = scaffold.scaffold_pack(tmp_path / "empty", name="empty", description="d")
    assert action_for(greenfield, "guard.py").verb == "created"


def test_dependency_floor_clamps_to_min_scaffold_floor(tmp_path: Path) -> None:
    # The dev checkout reports the 0.0.0 version sentinel, so only the clamp keeps the floor sane.
    assert scaffold.MIN_SCAFFOLD_FLOOR == "10.0.0"
    assert scaffold.dependency_floor() == f">={scaffold.MIN_SCAFFOLD_FLOOR}"


def test_install_snippet_keeps_marketplace_add_line(git_repo: Path) -> None:
    scaffold.scaffold_pack(git_repo, name="scratch", description="d")
    assert scaffold.install_snippet(git_repo, "scratch") == (
        "/plugin marketplace add yasyf/scratch",
        "/plugin install scratch@scratch",
    )


# --- refusals leave the tree untouched ------------------------------------------------


def test_unparseable_manifest_refuses_without_writing(tmp_path: Path) -> None:
    (root := tmp_path / "pkg").mkdir()
    (root / manager.PACK_MANIFEST).write_text("[[[ not valid toml")
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="pkg", description="d")
    assert manager.PACK_MANIFEST in str(exc.value)
    assert file_hashes(root) == before


def test_string_dependencies_refuses_without_writing(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "ccx", "dependencies": "captain-hook"}))
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="ccx", description="d")
    assert "plugin.json" in str(exc.value) and "dependencies" in str(exc.value)
    assert file_hashes(root) == before


def test_string_allowlist_refuses_without_writing(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "ccx", "allowCrossMarketplaceDependenciesOn": "captain-hook"})
    )
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="ccx", description="d")
    assert "marketplace.json" in str(exc.value)
    assert file_hashes(root) == before


def test_existing_skip_marked_guard_is_never_clobbered(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    write_manifest(root, name="pkg", hooks="hooks")
    (hooks := root / "hooks").mkdir()
    sentinel = "__capt_hook_skip__ = True\ndisabled_guard = 1\n"
    (guard := hooks / "guard.py").write_text(sentinel)
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert not any(a.path.name == "guard.py" for a in actions)  # the starter is never planned
    assert guard.read_text() == sentinel  # the disabled guard survives byte-for-byte


def test_scaffold_child_does_not_rewrite_parent_artifacts(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    (plugin_dir := parent / ".claude-plugin").mkdir(parents=True)
    plugin_before = {"name": "parent", "dependencies": [{"name": "unrelated"}]}
    market_before = {"name": "parent", "owner": {"name": "x"}, "allowCrossMarketplaceDependenciesOn": ["other"]}
    (plugin_dir / "plugin.json").write_text(json.dumps(plugin_before))
    (plugin_dir / "marketplace.json").write_text(json.dumps(market_before))
    scaffold.scaffold_pack(parent / "child", name="child", description="d")
    assert (parent / "child" / ".claude-plugin" / "plugin.json").is_file()  # the child got its own
    assert read_json(plugin_dir / "plugin.json") == plugin_before  # the parent's is untouched
    assert read_json(plugin_dir / "marketplace.json") == market_before


def test_hooks_dir_escaping_pack_root_refuses(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / manager.PACK_MANIFEST).write_text(
        '[pack]\nname = "pkg"\nversion = "0.1.0"\ndescription = "d"\nhooks = "../escape"\n'
    )
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="pkg", description="d")
    assert manager.PACK_MANIFEST in str(exc.value)
    assert file_hashes(root) == before
    assert not (tmp_path / "escape").exists()  # nothing landed outside the pack root


# --- CLI surface (exit code / install snippet gated on the Phase 4 lint rewire) -------


def test_cli_scaffold_writes_pack_grammar_manifest(tmp_path: Path) -> None:
    # The command's exit code and install snippet ride the Phase 4 lint rewrite; the artifacts land
    # regardless (scaffold writes before linting), so this asserts the wiring, not the exit code.
    root = tmp_path / "my-guards"
    run_cli("pack", "scaffold", str(root))
    manifest = manager.PackManifest.load(manager.manifest_in(root))
    assert manifest.name == "my-guards"  # --name defaults to the directory basename
    assert no_hooks_json(root)


def test_cli_explicit_name_conflicting_with_manifest_errors(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    write_manifest(root, name="realname")
    result = run_cli("pack", "scaffold", str(root), "--name", "otherwise")
    assert result.returncode != 0
    assert "conflicts" in result.stdout + result.stderr
