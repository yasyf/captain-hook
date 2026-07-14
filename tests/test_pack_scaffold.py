from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click
import pytest

from captain_hook.cli import DEFAULT_PREFIX
from captain_hook.packs import manager, scaffold
from captain_hook.packs.contract import command_entries
from tests.helpers import run_cli
from tests.test_pack_lint import (
    CANONICAL_ATTACH,
    POST_TOOL_HOOK,
    by_check,
    conforming,
    failed,
    legacy_getaway_hooks_json,
    write_hooks_json,
    write_manifest,
    write_plugin_json,
)

pytestmark = pytest.mark.usefixtures("isolate_modules")

ARTIFACTS = {"capt-hook.toml", "hooks.json", "plugin.json", "marketplace.json", "guard.py"}


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


# --- greenfield: create everything, lint green ---------------------------------------


def test_empty_dir_scaffolds_every_artifact_and_lints_green(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert verbs(actions) == ["created"] * 5
    assert {a.path.name for a in actions} == ARTIFACTS
    assert failed(by_check(root)) == []


def test_starter_hook_inline_tests_pass_via_cli(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    scaffold.scaffold_pack(root, name="pkg", description="d")
    result = run_cli("test", hooks_dir=str(root / "hooks"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout and "FAIL" not in result.stdout


def test_missing_manifest_written_from_template(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    scaffold.scaffold_pack(root, name="myfilter", description="My filter pack")
    manifest = manager.PackManifest.load(manager.manifest_in(root))
    assert (manifest.name, manifest.description, manifest.hooks) == ("myfilter", "My filter pack", "hooks")


def test_missing_hooks_json_creates_attach_only(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    write_manifest(root, name="pkg", hooks="hooks")
    (hooks := root / "hooks").mkdir()
    (hooks / "h.py").write_text(POST_TOOL_HOOK)
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert action_for(actions, "hooks.json").verb == "created"
    assert command_entries(read_json(hooks / "hooks.json")) == [("SessionStart", CANONICAL_ATTACH)]


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


# --- hooks.json migration ------------------------------------------------------------


def test_strips_legacy_mirrored_run_entries(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(
        root,
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": CANONICAL_ATTACH}]}],
                "Stop": [{"hooks": [{"type": "command", "command": f"{DEFAULT_PREFIX} run Stop"}]}],
                "PostToolUse": [
                    {"matcher": "Skill", "hooks": [{"type": "command", "command": f"{DEFAULT_PREFIX} run PostToolUse"}]}
                ],
            }
        },
    )
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "hooks.json").verb == "updated"
    assert command_entries(read_json(root / "hooks" / "hooks.json")) == [("SessionStart", CANONICAL_ATTACH)]
    assert failed(by_check(root)) == []


def test_migrates_bare_uvx_legacy_shape(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, legacy_getaway_hooks_json())
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "hooks.json").verb == "updated"
    assert command_entries(read_json(root / "hooks" / "hooks.json")) == [("SessionStart", CANONICAL_ATTACH)]
    assert failed(by_check(root)) == []


def test_preserves_foreign_entries_and_appends_attach(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(
        root,
        {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Skill", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/setup.sh"}]}
                ]
            }
        },
    )
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "hooks.json").verb == "updated"
    data = read_json(root / "hooks" / "hooks.json")
    assert data["hooks"]["PostToolUse"][0] == {
        "matcher": "Skill",
        "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/setup.sh"}],
    }
    assert ("SessionStart", CANONICAL_ATTACH) in command_entries(data)
    assert failed(by_check(root)) == []


# --- refusals leave the tree untouched ------------------------------------------------


def test_unparseable_manifest_refuses_without_writing(tmp_path: Path) -> None:
    (root := tmp_path / "pkg").mkdir()
    (root / manager.PACK_MANIFEST).write_text("[[[ not valid toml")
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="pkg", description="d")
    assert manager.PACK_MANIFEST in str(exc.value)
    assert file_hashes(root) == before


def test_unrecognized_capt_hook_entry_refuses_before_any_write(tmp_path: Path) -> None:
    (hooks := tmp_path / "pkg" / "hooks").mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "echo capt-hook run PostToolUse"}]}]}}
        )
    )
    root = tmp_path / "pkg"
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="pkg", description="d")
    assert "can't safely rewrite" in str(exc.value)
    assert not (root / manager.PACK_MANIFEST).is_file()  # planned first, but never written
    assert file_hashes(root) == before


def test_unparseable_hooks_json_refuses_without_writing(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / "hooks" / "hooks.json").write_text("{ not json ")
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="ccx", description="d")
    assert "not valid JSON" in str(exc.value)
    assert file_hashes(root) == before


# --- plugin.json / marketplace.json surgical upsert ----------------------------------


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
    assert failed(by_check(root)) == []


def test_marketplace_allowlist_gains_captain_hook_and_leaves_owner(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "ccx", "owner": {"name": "Someone"}, "allowCrossMarketplaceDependenciesOn": ["other"]})
    )
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "marketplace.json").verb == "updated"
    data = read_json(root / ".claude-plugin" / "marketplace.json")
    assert data["allowCrossMarketplaceDependenciesOn"] == ["other", "captain-hook"]
    assert data["owner"] == {"name": "Someone"}
    assert failed(by_check(root)) == []


# --- starter hook + version floor ----------------------------------------------------


def test_starter_hook_only_seeded_when_hooks_dir_empty(tmp_path: Path) -> None:
    with_hook = scaffold.scaffold_pack(conforming(tmp_path / "ccx"), name="ccx", description="d")
    assert not any(a.path.name == "guard.py" for a in with_hook)
    greenfield = scaffold.scaffold_pack(tmp_path / "empty", name="empty", description="d")
    assert action_for(greenfield, "guard.py").verb == "created"


def test_dependency_floor_clamps_to_min_scaffold_floor(tmp_path: Path) -> None:
    # The dev checkout reports the 0.0.0 version sentinel, so only the clamp keeps the floor sane.
    assert scaffold.dependency_floor() == f">={scaffold.MIN_SCAFFOLD_FLOOR}"


def test_install_snippet_derives_owner_repo_from_github_origin(git_repo: Path) -> None:
    scaffold.scaffold_pack(git_repo, name="scratch", description="d")
    assert scaffold.install_snippet(git_repo, "scratch") == (
        "/plugin marketplace add yasyf/scratch",
        "/plugin install scratch@scratch",
    )


# --- CLI surface ---------------------------------------------------------------------


def test_cli_scaffold_exits_zero_and_prints_install_snippet(tmp_path: Path) -> None:
    result = run_cli("pack", "scaffold", str(tmp_path / "pkg"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "created" in result.stdout
    assert "/plugin marketplace add" in result.stdout
    assert "/plugin install pkg@" in result.stdout
    assert "0 failed" in result.stdout


def test_cli_name_defaults_to_directory_basename(tmp_path: Path) -> None:
    root = tmp_path / "my-guards"
    assert run_cli("pack", "scaffold", str(root)).returncode == 0
    assert manager.PackManifest.load(manager.manifest_in(root)).name == "my-guards"


def test_cli_explicit_name_conflicting_with_manifest_errors(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    write_manifest(root, name="realname")
    result = run_cli("pack", "scaffold", str(root), "--name", "otherwise")
    assert result.returncode != 0
    assert "conflicts" in result.stdout + result.stderr


def test_cli_scaffold_exits_nonzero_when_lint_fails(tmp_path: Path) -> None:
    (hooks := tmp_path / "pkg" / "hooks").mkdir(parents=True)
    (hooks / "racy.py").write_text('from captain_hook import Event, hook\n\nhook(Event.SessionStart, message="racy")\n')
    result = run_cli("pack", "scaffold", str(tmp_path / "pkg"))
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert "session-start" in result.stdout
    assert "/plugin install" not in result.stdout  # the snippet is withheld on a failing lint


# --- fix A: non-list dependencies is invalid input, refuse (never spread a string into char-deps) --


def test_string_dependencies_refuses_without_writing(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "ccx", "dependencies": "captain-hook"}))
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="ccx", description="d")
    assert "plugin.json" in str(exc.value) and "dependencies" in str(exc.value)
    assert file_hashes(root) == before


# --- fix B: the captain-hook dep is identified by name, never by a foreign dep's marketplace field --


def test_foreign_dep_claiming_marketplace_is_left_untouched(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "other-plugin", "marketplace": "captain-hook"}])
    assert action_for(scaffold.scaffold_pack(root, name="ccx", description="d"), "plugin.json").verb == "updated"
    deps = read_json(root / ".claude-plugin" / "plugin.json")["dependencies"]
    assert {"name": "other-plugin", "marketplace": "captain-hook"} in deps  # the foreign dep is preserved
    captain = next(d for d in deps if d.get("name") == "captain-hook")
    assert captain == {"name": "captain-hook", "marketplace": "captain-hook", "version": scaffold.dependency_floor()}


# --- fix C: non-list allowlist is invalid input, refuse (never coerce to [] then overwrite) ---------


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


# --- fix D: malformed hooks.json shapes refuse loud instead of tracebacking on AttributeError -------


@pytest.mark.parametrize(
    "bad_hooks",
    [
        {"PostToolUse": "not-a-list"},
        {"PostToolUse": ["not-a-dict"]},
        {"PostToolUse": [{"hooks": "not-a-list"}]},
        {"PostToolUse": [{"hooks": ["not-a-dict"]}]},
        {"PostToolUse": [{"hooks": [{"type": "command"}]}]},
    ],
    ids=["non-list-groups", "non-dict-group", "non-list-entries", "non-dict-entry", "command-without-command"],
)
def test_malformed_hooks_shape_refuses_without_writing(tmp_path: Path, bad_hooks: dict) -> None:
    root = conforming(tmp_path / "ccx")
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": bad_hooks}))
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="ccx", description="d")
    assert "hooks.json" in str(exc.value)
    assert file_hashes(root) == before


# --- fix E: an existing guard.py is never clobbered, even skip-marked (has_hook_files excludes it) --


def test_existing_skip_marked_guard_is_never_clobbered(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    write_manifest(root, name="pkg", hooks="hooks")
    (hooks := root / "hooks").mkdir()
    sentinel = "__capt_hook_skip__ = True\ndisabled_guard = 1\n"
    (guard := hooks / "guard.py").write_text(sentinel)
    actions = scaffold.scaffold_pack(root, name="pkg", description="d")
    assert not any(a.path.name == "guard.py" for a in actions)  # the starter is never planned
    assert guard.read_text() == sentinel  # the disabled guard survives byte-for-byte


# --- fix F: a capt-hook entry carrying a shell operator is unrecognized, so scaffold refuses --------


@pytest.mark.parametrize(
    "trailing",
    [" && /opt/audit", "&&/opt/audit", " | tee /opt/log", "; /opt/audit", " > /opt/out"],
    ids=["and-spaced", "and-glued", "pipe", "semicolon", "redirect"],
)
def test_compound_run_entry_refuses_never_strips(tmp_path: Path, trailing: str) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(
        root,
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": CANONICAL_ATTACH}]}],
                "Stop": [{"hooks": [{"type": "command", "command": f"{DEFAULT_PREFIX} run Stop{trailing}"}]}],
            }
        },
    )
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="ccx", description="d")
    assert "can't safely rewrite" in str(exc.value)  # refuses rather than deleting the foreign command
    assert file_hashes(root) == before


# --- fix I: scaffolding a child never ascends above its root to rewrite a parent's plugin artifacts -


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


# --- fix J: a hooks dir escaping the pack root refuses before writing a starter outside the pack ----


def test_hooks_dir_escaping_pack_root_refuses(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / manager.PACK_MANIFEST).write_text(
        'name = "pkg"\nversion = "0.1.0"\ndescription = "d"\nhooks = "../escape"\n'
    )
    before = file_hashes(root)
    with pytest.raises(click.ClickException) as exc:
        scaffold.scaffold_pack(root, name="pkg", description="d")
    assert manager.PACK_MANIFEST in str(exc.value)
    assert file_hashes(root) == before
    assert not (tmp_path / "escape").exists()  # nothing landed outside the pack root


# --- fix K: a foreign group with no capt-hook entries is preserved verbatim, not pruned as empty ----


def test_empty_foreign_group_preserved_verbatim(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(
        root,
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": CANONICAL_ATTACH}]}],
                "PostToolUse": [{"matcher": "Skill", "hooks": []}],
            }
        },
    )
    scaffold.scaffold_pack(root, name="ccx", description="d")
    data = read_json(root / "hooks" / "hooks.json")
    assert data["hooks"].get("PostToolUse") == [{"matcher": "Skill", "hooks": []}]  # kept, not pruned
