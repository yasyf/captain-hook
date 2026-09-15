from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from captain_hook.packs import manager, plugins
from captain_hook.util.paths import resolve_claude_config_dir
from tests.helpers import run_cli

HOOK_SRC = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message={message!r})\n"


def write_plugin_pack(
    tmp: Path,
    name: str,
    version: str = "1.0.0",
    *,
    slot: str | None = None,
    repository: str | None = None,
    hook_message: str | None = None,
    descriptor: str = "resources = []\n",
) -> Path:
    """A versioned cache-shaped plugin root shipping a pack at the fixed ``capt-hook/`` path."""
    root = tmp / "install" / (slot or name) / version
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    plugin_json: dict[str, object] = {
        "name": name,
        "version": version,
        "dependencies": [{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=11.0.0"}],
    }
    if repository is not None:
        plugin_json["repository"] = repository
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(plugin_json))
    (hooks := root / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / manager.PLUGIN_PACK_DIRNAME / manager.PACK_DESCRIPTOR).write_text(descriptor)
    (hooks / "h.py").write_text(HOOK_SRC.format(message=hook_message or name))
    return root


def install_record(root: Path, *, scope: str = "user", project_path: Path | None = None) -> dict[str, object]:
    record: dict[str, object] = {"scope": scope, "installPath": str(root), "version": root.name}
    if project_path is not None:
        record["projectPath"] = str(project_path)
    return record


def write_installed(installs: dict[str, object]) -> None:
    (path := plugins.installed_plugins_path()).parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": installs}))


def write_enabled(settings: Path, enabled: dict[str, bool]) -> None:
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"enabledPlugins": enabled}))


def user_settings() -> Path:
    return resolve_claude_config_dir() / "settings.json"


def install(installs: dict[str, object], *, enabled: bool = True) -> None:
    """Record ``installs`` in ``installed_plugins.json`` and set each id's user-scope enablement."""
    write_installed(installs)
    user_settings().write_text(json.dumps({"enabledPlugins": dict.fromkeys(installs, enabled)}))


def test_enabled_plugin_with_pack_loads(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show", repository="https://github.com/yasyf/show")
    install({"show@show": [install_record(plugin)]})

    (rp,) = plugins.resolve_plugin_packs(root)
    assert isinstance(rp.entry, manager.PluginPack)
    assert (rp.entry.plugin_id, rp.entry.root, rp.entry.repository) == (
        "show@show",
        str(plugin),
        "https://github.com/yasyf/show",
    )
    assert rp.pack_id == "plugin:show@show"
    assert rp.name == "show@show"
    assert rp.path == plugin / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME


def test_namespaced_identity_orders_by_plugin_id(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    a = write_plugin_pack(tmp_path, "a", slot="a")
    b = write_plugin_pack(tmp_path, "b", slot="b")
    install({"b@mkt": [install_record(b)], "a@mkt": [install_record(a)]})
    assert [rp.pack_id for rp in plugins.resolve_plugin_packs(root)] == ["plugin:a@mkt", "plugin:b@mkt"]


def test_plugin_without_repository_routes_none(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install({"show@show": [install_record(plugin)]})
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.entry.repository is None


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"enabledPlugins": {"show@show": False}}, id="disabled"),
        pytest.param({}, id="never-enabled"),
    ],
)
def test_plugin_not_enabled_is_skipped(tmp_path: Path, settings: dict[str, object]) -> None:
    (root := tmp_path / "proj").mkdir()
    write_installed({"show@show": [install_record(write_plugin_pack(tmp_path, "show"))]})
    user_settings().write_text(json.dumps(settings))

    assert plugins.enabled_plugins(root) == ()
    assert plugins.resolve_plugin_packs(root) == []


@pytest.mark.parametrize(
    ("layers", "enabled"),
    [
        pytest.param({"user": False, "project": True}, True, id="project-over-user"),
        pytest.param({"user": True, "project": True, "local": False}, False, id="local-over-project"),
        pytest.param({"user": True, "local": True, "managed": False}, False, id="managed-over-local"),
        pytest.param({"user": False, "dropin": True}, True, id="managed-dropin-over-user"),
    ],
)
def test_enablement_follows_the_settings_stack(tmp_path: Path, layers: dict[str, bool], enabled: bool) -> None:
    (root := tmp_path / "proj").mkdir()
    write_installed({"show@show": [install_record(write_plugin_pack(tmp_path, "show"))]})
    config = resolve_claude_config_dir()
    paths = {
        "user": config / "settings.json",
        "project": root / ".claude" / "settings.json",
        "local": root / ".claude" / "settings.local.json",
        "managed": config / "managed-settings.json",
        "dropin": config / "managed-settings.d" / "10-policy.json",
    }
    for layer, on in layers.items():
        write_enabled(paths[layer], {"show@show": on})

    assert bool(plugins.enabled_plugins(root)) is enabled


def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    (main := tmp_path / "main").mkdir()
    (gitdir := main / ".git" / "worktrees" / "lane").mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (worktree := tmp_path / "lanes" / "lane").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    return main, worktree


@pytest.mark.parametrize(
    ("main_local", "worktree_local", "enabled"),
    [
        pytest.param(True, None, True, id="main-checkout-local-file-applies-to-the-worktree"),
        pytest.param(None, True, True, id="legacy-worktree-local-file-still-read"),
        pytest.param(False, True, False, id="main-checkout-wins-over-legacy-worktree-file"),
        pytest.param(True, False, True, id="main-checkout-wins-either-way"),
    ],
)
def test_worktree_reads_the_main_checkouts_local_settings(
    tmp_path: Path, main_local: bool | None, worktree_local: bool | None, enabled: bool
) -> None:
    main, worktree = linked_worktree(tmp_path)
    write_installed({"show@show": [install_record(write_plugin_pack(tmp_path, "show"))]})
    write_enabled(user_settings(), {"show@show": not enabled})
    if main_local is not None:
        write_enabled(main / ".claude" / "settings.local.json", {"show@show": main_local})
    if worktree_local is not None:
        write_enabled(worktree / ".claude" / "settings.local.json", {"show@show": worktree_local})

    assert plugins.local_settings_root(worktree) == main.resolve()
    assert bool(plugins.enabled_plugins(worktree)) is enabled


def test_local_settings_root_is_the_repo_root_for_a_subdirectory(tmp_path: Path) -> None:
    (repo := tmp_path / "repo" / ".git").mkdir(parents=True)
    (sub := tmp_path / "repo" / "pkg").mkdir()
    (outside := tmp_path / "outside").mkdir()
    assert plugins.local_settings_root(sub) == repo.parent
    assert plugins.local_settings_root(outside) == outside


def test_plugin_without_pack_dir_skipped(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    (consumer := tmp_path / "install" / "consumer" / "1.0.0").mkdir(parents=True)
    install({"consumer@mkt": [install_record(consumer)]})
    assert plugins.resolve_plugin_packs(root) == []


def test_install_whose_directory_is_gone_is_skipped(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    good = write_plugin_pack(tmp_path, "good", slot="good")
    install({"good@mkt": [install_record(good)], "gone@mkt": [install_record(tmp_path / "install" / "gone" / "1")]})
    assert [p.id for p in plugins.enabled_plugins(root)] == ["good@mkt"]


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda pack: (pack / manager.PACK_DESCRIPTOR).unlink(), id="missing_descriptor"),
        pytest.param(lambda pack: shutil.rmtree(pack / manager.HOOKS_DIRNAME), id="missing_hooks_dir"),
        pytest.param(lambda pack: (pack / manager.PACK_DESCRIPTOR).write_text("name = = broken\n"), id="broken_toml"),
        pytest.param(
            lambda pack: (pack / manager.PACK_DESCRIPTOR).write_text("[tools.x]\n"), id="tool_missing_behaves_like"
        ),
        pytest.param(
            lambda pack: (pack / manager.PACK_DESCRIPTOR).write_text('resources = "not-a-list"\n'), id="bad_resources"
        ),
    ],
)
def test_malformed_pack_is_fatal(tmp_path: Path, mangle) -> None:  # type: ignore[no-untyped-def]
    (root := tmp_path / "proj").mkdir()
    good = write_plugin_pack(tmp_path, "good", slot="good")
    bad = write_plugin_pack(tmp_path, "bad", slot="bad")
    mangle(bad / manager.PLUGIN_PACK_DIRNAME)
    install({"good@mkt": [install_record(good)], "bad@mkt": [install_record(bad)]})
    with pytest.raises(manager.PackError):
        plugins.resolve_plugin_packs(root)


def test_identical_scoped_installs_collapse(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    plugin = write_plugin_pack(tmp_path, "show")
    install(
        {"show@mkt": [install_record(plugin, scope="project", project_path=root), install_record(plugin, scope="user")]}
    )
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.pack_id == "plugin:show@mkt"


def test_scope_precedence_project_over_user(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    proj = write_plugin_pack(tmp_path, "show", "9.9.9", slot="proj")
    user = write_plugin_pack(tmp_path, "show", "1.0.0", slot="user")
    install(
        {"show@mkt": [install_record(user, scope="user"), install_record(proj, scope="project", project_path=root)]}
    )
    (rp,) = plugins.resolve_plugin_packs(root)
    assert rp.entry.root == str(proj)


def test_same_scope_conflict_in_one_project_raises(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    a = write_plugin_pack(tmp_path, "show", slot="a")
    b = write_plugin_pack(tmp_path, "show", slot="b")
    install(
        {
            "show@mkt": [
                install_record(a, scope="project", project_path=root),
                install_record(b, scope="project", project_path=root),
            ]
        }
    )
    with pytest.raises(plugins.PluginListError, match="2 install paths at scope 'project'"):
        plugins.enabled_plugins(root)


def test_same_scope_in_different_projects_resolves_per_project(tmp_path: Path) -> None:
    (here := tmp_path / "here").mkdir()
    (there := tmp_path / "there").mkdir()
    mine = write_plugin_pack(tmp_path, "codex", "1.9.0", slot="mine")
    theirs = write_plugin_pack(tmp_path, "codex", "1.8.4", slot="theirs")
    install(
        {
            "codex@skills": [
                install_record(theirs, scope="local", project_path=there),
                install_record(mine, scope="local", project_path=here),
            ]
        }
    )
    (ours,) = plugins.resolve_plugin_packs(here)
    assert ours.entry.root == str(mine)
    (yours,) = plugins.resolve_plugin_packs(there)
    assert yours.entry.root == str(theirs)


def test_foreign_project_install_does_not_govern_this_root(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    (other := tmp_path / "other").mkdir()
    ours = write_plugin_pack(tmp_path, "show", slot="ours")
    foreign = write_plugin_pack(tmp_path, "show", slot="foreign")
    only_theirs = write_plugin_pack(tmp_path, "theirs", slot="theirs")
    install(
        {
            "show@mkt": [install_record(ours), install_record(foreign, scope="local", project_path=other)],
            "theirs@mkt": [install_record(only_theirs, scope="project", project_path=other)],
        }
    )
    (resolved,) = plugins.resolve_plugin_packs(root)
    assert resolved.entry.root == str(ours)


def test_removed_root_still_reads_its_user_scope_roster(tmp_path: Path) -> None:
    (removed := tmp_path / "scratch" / "gone").mkdir(parents=True)
    removed.rmdir()
    install({"show@mkt": [install_record(write_plugin_pack(tmp_path, "show"))]})
    assert len(plugins.enabled_plugins(removed)) == 1


def test_installed_plugins_absent_is_an_empty_roster(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    write_enabled(user_settings(), {"show@mkt": True})
    assert plugins.enabled_plugins(root) == ()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json {", id="unparseable"),
        pytest.param("[]", id="top-level-array"),
        pytest.param('{"version": 2, "plugins": []}', id="plugins-not-an-object"),
    ],
)
def test_unreadable_roster_raises(tmp_path: Path, content: str) -> None:
    (root := tmp_path / "proj").mkdir()
    plugins.installed_plugins_path().write_text(content)
    with pytest.raises(plugins.PluginListError):
        plugins.enabled_plugins(root)


def test_malformed_install_records_do_not_suppress_valid_siblings(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    good = write_plugin_pack(tmp_path, "show")
    install(
        {
            "show@mkt": [123, {"scope": "user"}, {"installPath": 7}, install_record(good)],
            "other@mkt": "not-a-list",
        }
    )
    (only,) = plugins.enabled_plugins(root)
    assert (only.id, only.root) == ("show@mkt", str(good))


def test_newer_plugin_version_rebinds_on_the_next_read(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    v1 = write_plugin_pack(tmp_path, "show", "1.0.0")
    install({"show@mkt": [install_record(v1)]})
    (first,) = plugins.resolve_plugin_packs(root)
    assert first.entry.root == str(v1)

    v2 = write_plugin_pack(tmp_path, "show", "2.0.0")
    install({"show@mkt": [install_record(v2)]})
    (second,) = plugins.resolve_plugin_packs(root)
    assert second.entry.root == str(v2)
    assert second.path == v2 / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME


def test_roster_failure_is_recorded_where_a_person_looks(tmp_path: Path, logcap) -> None:  # type: ignore[no-untyped-def]
    from captain_hook import faults
    from captain_hook.app import _state
    from captain_hook.cli import PLUGIN_ROSTER_SOURCE, CliState

    (root := tmp_path / "proj").mkdir()
    plugins.installed_plugins_path().write_text("not json {")

    packs = CliState(root=root).discover()

    assert packs and all(isinstance(p.entry, manager.BuiltinPack) for p in packs)
    (error,) = _state.load_errors
    assert error.source == PLUGIN_ROSTER_SOURCE and isinstance(error.exc, plugins.PluginListError)
    assert [r for r in logcap.records if r.levelno == logging.ERROR and "plugin roster unusable" in r.message]
    (line,) = faults.drain()
    assert line.startswith(faults.ANNOUNCE_PREFIX) and PLUGIN_ROSTER_SOURCE in line


def run_dispatch(root: Path, event: str, **raw: object) -> str:
    stdin = json.dumps({"session_id": "e2e-sess", **raw})
    result = run_cli("run", event, root_dir=str(root), stdin_data=stdin)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_e2e_discovered_plugin_pack_hook_fires_once(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    install({"show@mkt": [install_record(write_plugin_pack(tmp_path, "show"))]})

    out = run_dispatch(root, "PreToolUse", tool_name="Bash", tool_input={"command": "echo hi"})
    assert out.count("show") == 1


def test_e2e_discovered_plugin_pack_fires_for_subagent_event(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    install({"show@mkt": [install_record(write_plugin_pack(tmp_path, "show"))]})

    out = run_dispatch(root, "PreToolUse", tool_name="Bash", tool_input={"command": "echo hi"}, agent_id="sub-1")
    assert "show" in out


def test_test_subcommand_never_resolves_plugin_packs(tmp_path: Path, isolate_modules: None) -> None:
    (root := tmp_path / "proj").mkdir()
    (local_hooks := root / ".claude" / "hooks").mkdir(parents=True)
    (local_hooks / "__init__.py").write_text("")
    (local_hooks / "good.py").write_text(
        "from captain_hook.app import hook\n"
        "from captain_hook.types import Event\n"
        "from captain_hook.testing.types import Block, Input\n\n"
        'hook(Event.PreToolUse, message="ok", block=True, tests={Input(command="echo hi"): Block()})\n'
    )
    plugin = write_plugin_pack(tmp_path, "redpack")
    (plugin / manager.PLUGIN_PACK_DIRNAME / manager.HOOKS_DIRNAME / "h.py").write_text(
        "from captain_hook.app import hook\n"
        "from captain_hook.types import Event\n"
        "from captain_hook.testing.types import Allow, Input\n\n"
        'hook(Event.PreToolUse, message="RED", block=True, tests={Input(command="echo hi"): Allow()})\n'
    )
    install({"redpack@mkt": [install_record(plugin)]})

    result = run_cli("test", root_dir=str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 tests: 1 passed" in result.stdout
    assert "RED" not in result.stdout

    from captain_hook.cli import CliState

    assert "plugin:redpack@mkt" in {p.pack_id for p in CliState(root=root).discover()}


def test_pack_list_renders_builtin_and_plugin_ids(tmp_path: Path) -> None:
    (root := tmp_path / "proj").mkdir()
    install({"show@mkt": [install_record(write_plugin_pack(tmp_path, "show"))]})

    result = run_cli("pack", "list", root_dir=str(root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "builtin:general" in result.stdout
    assert "plugin:show@mkt" in result.stdout
