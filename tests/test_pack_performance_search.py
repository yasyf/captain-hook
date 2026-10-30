from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from captain_hook.cmd import Target
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack
from captain_hook.packs import manager
from captain_hook.types import Event
from tests.helpers import make_pre_tool_event, run_cli
from tests.test_cmd import evt_for

ORCA_UUID = "7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17"
LINKED_UUID = "0b9e4d22-8a71-4c3f-b5e6-91d0c7a3f258"


@pytest.fixture
def performance(isolate_modules: None) -> None:
    discover_pack("performance", manager.resolve_builtin("performance").path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "internal" / "focused").mkdir(parents=True)
    (root / "infra").mkdir()
    (root / "explicit.log").write_text("needle\n")
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.resolve() / "home"
    (root / ".claude" / "scratch" / "release-v3" / "specific-lane").mkdir(parents=True)
    (root / ".claude" / "scratch" / "durable-agents-rebuild").mkdir()
    (root / ".claude" / "scratch" / "notes.log").write_text("needle\n")
    (lane := root / ".claude" / "worktrees" / "captain-hook" / "lane" / "src").mkdir(parents=True)
    (lane.parent / ".git").write_text("gitdir: ../actual")
    (root / ".claude" / "worktrees" / "README").write_text("pool notes\n")
    (local := root / "project" / ".claude" / "worktrees" / "lane" / "src").mkdir(parents=True)
    (local.parent / ".git").write_text("gitdir: ../../../.git/worktrees/lane")
    (local.parents[1] / "stale").mkdir()
    (adopted := root / "repos" / "orca" / "workspaces" / ORCA_UUID / "captain-hook" / "lane" / "src").mkdir(
        parents=True
    )
    (adopted.parent / ".git").mkdir()
    (cleanup := root / ".daemonkit" / "a" / "ccx-cleanup").mkdir(parents=True)
    (cleanup / "switch.json").write_text('{"paused":true}')
    (job := cleanup / "jobs" / "queued-job").mkdir(parents=True)
    (job / "job.json").write_text("{}")
    (payload := job / "payload" / "src").mkdir(parents=True)
    (payload.parent / ".git").write_text("gitdir: /retired")
    monkeypatch.setenv("HOME", str(root))
    return root


@pytest.fixture
def linked_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (real := tmp_path.resolve() / "real-home" / "project").mkdir(parents=True)
    (link := tmp_path.resolve() / "linked-home").symlink_to(real.parent)
    monkeypatch.setenv("HOME", str(link))
    return link


@pytest.fixture
def relocated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (real := tmp_path.resolve() / "Volumes" / "data" / "user" / "project").mkdir(parents=True)
    (users := tmp_path.resolve() / "Users").mkdir()
    (link := users / "user").symlink_to(real.parent)
    monkeypatch.setenv("HOME", str(link))
    return link


@pytest.fixture
def relocated_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (lane := tmp_path.resolve() / "elsewhere" / "worktrees" / "captain-hook" / "lane").mkdir(parents=True)
    (lane / ".git").write_text("gitdir: ../actual")
    (claude := tmp_path.resolve() / "home" / ".claude").mkdir(parents=True)
    (claude / "worktrees").symlink_to(lane.parents[1])
    monkeypatch.setenv("HOME", str(claude.parent))
    return claude.parent


@pytest.fixture
def linked_aggregates(home: Path, tmp_path: Path) -> Path:
    (aggregate := tmp_path.resolve() / "outside" / "aggregate" / "captain-hook" / "lane").mkdir(parents=True)
    (checkout := tmp_path.resolve() / "outside" / "checkout" / "src").mkdir(parents=True)
    (checkout.parent / ".git").mkdir()
    pool = home / "repos" / "orca" / "workspaces"
    (pool / LINKED_UUID).symlink_to(aggregate.parents[1])
    (pool / ORCA_UUID / "linked-repo").symlink_to(aggregate.parent)
    (pool / ORCA_UUID / "linked-checkout").symlink_to(checkout.parent)
    return pool


@pytest.fixture
def indexed(git_repo: Path) -> Path:
    root = git_repo.resolve()
    (focused := root / "internal" / "focused").mkdir(parents=True)
    (focused / "inside.txt").write_text("needle\n")
    (root / "outside.txt").write_text("needle\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return focused


def decision(command: str, cwd: Path, session_dir: Path) -> str | None:
    result = dispatch(Event.PreToolUse, evt_for(command, cwd=str(cwd)), session_dir=session_dir / "session")
    if result is None:
        return None
    output = result["hookSpecificOutput"]
    return output.get("permissionDecision") if "additionalContext" not in output else "warn"


@pytest.mark.parametrize(
    "tool_name",
    [
        "ccx_code_grep",
        "mcp__cc_context__ccx_code_grep",
        "mcp__cc-context__ccx_code_grep",
        "mcp__plugin_cc-context_cc-context__ccx_code_grep",
    ],
)
def test_structured_mcp_grep_uses_existing_scope_guard_through_dispatch(
    performance: None, repo: Path, tmp_path: Path, tool_name: str
) -> None:
    event = make_pre_tool_event(tool_name, {"text": "needle", "paths": [str(repo)], "budget": 1})
    event._raw["cwd"] = str(repo / "internal" / "focused")
    result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
    assert result is not None
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "output `budget` does not bound" in output["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"paths": ["/"]}, "deny", id="filesystem-root"),
        pytest.param({"paths": ["~"]}, None, id="tilde-path-is-not-authoritative"),
        pytest.param({"repo": "~"}, None, id="tilde-repo-is-not-authoritative"),
        pytest.param({}, "deny", id="explicit-absolute-repo-root"),
        pytest.param({"paths": ["."]}, "deny", id="explicit-repo-root"),
        pytest.param({"paths": ["internal/focused", "."]}, "deny", id="narrow-path-does-not-hide-root"),
        pytest.param({"paths": ["~/.claude/worktrees"]}, None, id="tilde-pool-is-not-authoritative"),
        pytest.param({"paths": ["explicit.log"]}, None, id="explicit-file"),
        pytest.param({"paths": ["internal/focused"]}, None, id="focused-directory"),
        pytest.param({"paths": ["internal/focused"], "budget": 1}, None, id="bounded-path-with-output-budget"),
        pytest.param({"globs": ["*.py"]}, None, id="filename-bound"),
        pytest.param({"paths": ["~"], "globs": ["*.py"]}, None, id="filename-bound-over-home"),
        pytest.param({"globs": ["!*.md"]}, "deny", id="exclusions-are-not-a-bound"),
        pytest.param({"globs": ["*.py", "**"]}, "deny", id="broad-glob-beside-narrow"),
        pytest.param({"paths": ["."], "budget": 1}, "deny", id="output-budget-is-not-a-bound"),
        pytest.param({"paths": ["."], "regex": True, "filesWithMatches": True}, "deny", id="output-mode-is-not-a-bound"),
        pytest.param({"repo": ".", "paths": ["internal/focused"]}, None, id="relative-repo-and-path"),
        pytest.param({"paths": "internal/focused"}, "deny", id="malformed-paths"),
        pytest.param({"paths": ["internal/focused", None]}, "deny", id="incomplete-paths"),
        pytest.param({"globs": "*.py"}, "deny", id="malformed-globs"),
    ],
)
def test_structured_mcp_grep_reads_caller_fields_without_shell_reparsing(
    performance: None, repo: Path, home: Path, tmp_path: Path, fields: dict[str, object], expected: str | None
) -> None:
    event = make_pre_tool_event(
        "mcp__plugin_cc-context_cc-context__ccx_code_grep", {"text": "needle", "repo": str(repo)} | fields
    )
    event._raw["cwd"] = str(repo)
    result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
    assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected


def test_structured_mcp_grep_resolves_paths_against_explicit_repo(
    performance: None, repo: Path, home: Path, tmp_path: Path
) -> None:
    (home / "explicit.log").write_text("needle\n")
    for fields, expected in [
        ({"repo": str(home), "paths": ["explicit.log"]}, None),
        ({"repo": str(repo), "paths": ["."]}, "deny"),
        ({"repo": str(repo), "paths": ["internal/focused"]}, None),
    ]:
        event = make_pre_tool_event("mcp__cc_context__ccx_code_grep", {"text": "needle"} | fields)
        event._raw["cwd"] = str(repo / "internal" / "focused")
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected


def test_structured_mcp_grep_keeps_literal_paths_and_other_tools_scoped(
    performance: None, repo: Path, tmp_path: Path
) -> None:
    literal = repo / "explicit $literal; file.log"
    literal.write_text("needle\n")
    for tool_name, fields in [
        ("mcp__cc_context__ccx_code_grep", {"text": "needle", "paths": [str(literal)]}),
        ("mcp__cc_context__ccx_code_grep", {"text": "~ / Code", "paths": ["internal/focused"]}),
        ("mcp__other__code_grep", {"text": "needle", "paths": [str(repo)]}),
        ("mcp__cc_context__ccx_code_read", {"path": str(repo)}),
    ]:
        event = make_pre_tool_event(tool_name, {"repo": str(repo)} | fields)
        event._raw["cwd"] = str(repo)
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") is None


def test_structured_mcp_grep_follows_repository_path_aliases(
    performance: None, repo: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(repo, target_is_directory=True)
    nested_alias = tmp_path / "entry" / "checkout"
    nested_alias.parent.mkdir()
    nested_alias.symlink_to(repo, target_is_directory=True)
    (nested_alias.parent / "sibling" / ".git").mkdir(parents=True)
    (tmp_path / "sibling").mkdir()
    for fields, expected in [
        ({"paths": [str(alias)]}, "deny"),
        ({"repo": str(alias), "paths": ["."]}, "deny"),
        ({"repo": str(alias), "paths": ["internal/focused"]}, None),
        ({"repo": str(nested_alias), "paths": ["../sibling"]}, "deny"),
        ({"paths": [str(nested_alias / ".." / "sibling")]}, None),
    ]:
        event = make_pre_tool_event("mcp__cc_context__ccx_code_grep", {"text": "needle"} | fields)
        event._raw["cwd"] = str(repo / "internal" / "focused")
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected


def test_structured_mcp_grep_classifies_without_walks_subprocesses_or_history(
    performance: None, repo: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: object, **__: object) -> NoReturn:
        raise AssertionError("structured search scope classification must not walk, spawn, or read history")

    monkeypatch.setattr(Target, "expand", forbidden)
    monkeypatch.setattr("captain_hook.cmd.contains_repo", forbidden)
    monkeypatch.setattr("captain_hook.cmd.glob_matches", forbidden)
    monkeypatch.setattr("captain_hook.util.vcs.scanned_names", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("captain_hook.transcripts.registered_sources", forbidden)
    for path, expected in [(str(repo), "deny"), (str(home / ".claude/worktrees"), "deny"), ("internal/focused", None)]:
        event = make_pre_tool_event(
            "mcp__cc_context__ccx_code_grep", {"text": "needle", "repo": str(repo), "paths": [path]}
        )
        event._raw["cwd"] = str(repo)
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="default-workspace-is-unknown"),
        pytest.param({"paths": ["."]}, id="relative-root-is-unknown"),
        pytest.param({"paths": ["internal/focused"]}, id="relative-directory-is-unknown"),
        pytest.param({"paths": ["explicit.log"]}, id="relative-file-is-unknown"),
        pytest.param({"repo": "."}, id="relative-repo-is-unknown"),
        pytest.param({"repo": "../..", "paths": ["."]}, id="relative-repo-and-path-unknown"),
        pytest.param({"repo": "~", "paths": ["."]}, id="tilde-repo-is-not-expanded"),
        pytest.param({"paths": ["~"]}, id="tilde-path-is-not-expanded"),
        pytest.param({"paths": ["~/project"]}, id="tilde-subtree-is-not-expanded"),
    ],
)
def test_structured_mcp_grep_preserves_unknown_workspace_scope(
    performance: None, repo: Path, tmp_path: Path, fields: dict[str, object]
) -> None:
    for cwd in [repo, repo / "internal" / "focused"]:
        event = make_pre_tool_event("mcp__cc_context__ccx_code_grep", {"text": "needle"} | fields)
        event._raw["cwd"] = str(cwd)
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") is None


def test_structured_mcp_grep_classifies_absolute_operands_even_with_unknown_relative_ones(
    performance: None, repo: Path, home: Path, tmp_path: Path
) -> None:
    for fields, expected in [
        ({"paths": [str(repo), "internal/focused"]}, "deny"),
        ({"repo": "relative-project", "paths": [str(home)]}, "deny"),
        ({"paths": [str(repo / "internal/focused"), "."]}, None),
        ({"repo": str(repo), "paths": ["."]}, "deny"),
        ({"repo": str(repo), "paths": ["internal/focused"]}, None),
    ]:
        event = make_pre_tool_event("mcp__cc_context__ccx_code_grep", {"text": "needle"} | fields)
        event._raw["cwd"] = str(repo / "internal" / "focused")
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected


@pytest.fixture
def search_provenance(performance: None, home: Path) -> dict[str, Path]:
    collection = home / "Code"
    primary = collection / "monorepo"
    git = primary / ".git"
    worktree_git = git / "worktrees" / "amberjack"
    worktree_git.mkdir(parents=True)
    (worktree_git / "commondir").write_text("../..\n")
    checkout = home / ".orca" / "workspaces" / "monorepo" / "monorepo" / "amberjack"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {worktree_git}\n")
    for directory in [checkout / ".claude", checkout / ".agents", primary / "src" / "focused"]:
        directory.mkdir(parents=True)
    plugins = home / ".claude" / "plugins"
    loaded = plugins / "cache" / "fixture-marketplace" / "long-running" / "1.0" / "capt-hook" / "hooks"
    loaded.mkdir(parents=True)
    module = loaded / "fixture.py"
    module.write_text(
        "from captain_hook import Event, Tool, hook\n"
        "hook(Event.PreToolUse, only_if=[Tool('LoadedPackFixture')], message='fixture')\n"
    )
    discover_pack("provenance-fixture", loaded)
    (loaded / "focused").mkdir()
    (collection / "collection-note.txt").write_text("fixture needle\n")
    (plugins / "unloaded-subtree").mkdir()
    collection_alias = home / "collection-alias"
    collection_alias.symlink_to(collection, target_is_directory=True)
    plugins_alias = home / "pack-alias"
    plugins_alias.symlink_to(plugins, target_is_directory=True)
    return {
        "collection": collection,
        "primary": primary,
        "checkout": checkout,
        "plugins": plugins,
        "loaded": loaded,
        "collection_alias": collection_alias,
        "plugins_alias": plugins_alias,
    }


def test_structured_mcp_grep_blocks_the_captured_four_root_payload_by_known_provenance(
    search_provenance: dict[str, Path], tmp_path: Path
) -> None:
    roots = search_provenance
    event = make_pre_tool_event(
        "mcp__plugin_cc-context_cc-context__ccx_code_grep",
        {
            "text": "fixture needle",
            "paths": [
                str(roots["collection"]),
                str(roots["plugins"]),
                str(roots["checkout"] / ".claude"),
                str(roots["checkout"] / ".agents"),
            ],
            "budget": 3000,
        },
    )
    event._raw["cwd"] = str(roots["checkout"])
    result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ancestor of a known checkout or loaded pack" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("role", "suffix", "expected"),
    [
        pytest.param("collection", "", "deny", id="common-checkout-ancestor"),
        pytest.param("plugins", "", "deny", id="loaded-pack-ancestor"),
        pytest.param("primary", "", "deny", id="common-checkout-root"),
        pytest.param("checkout", "", "deny", id="current-checkout-root"),
        pytest.param("loaded", "", "deny", id="declared-loaded-pack-root"),
        pytest.param("collection_alias", "", "deny", id="common-ancestor-alias"),
        pytest.param("plugins_alias", "", "deny", id="loaded-ancestor-alias"),
        pytest.param("checkout", ".claude", None, id="current-checkout-narrow-config"),
        pytest.param("checkout", ".agents", None, id="current-checkout-narrow-agents"),
        pytest.param("primary", "src/focused", None, id="common-checkout-focused-descendant"),
        pytest.param("loaded", "focused", None, id="loaded-pack-focused-descendant"),
        pytest.param("collection", "collection-note.txt", None, id="explicit-file-under-container"),
        pytest.param("loaded", "fixture.py", None, id="explicit-loaded-file"),
        pytest.param("plugins", "unloaded-subtree", None, id="unrelated-container-descendant"),
        pytest.param("collection_alias", "monorepo/src/focused", None, id="focused-descendant-through-alias"),
    ],
)
def test_known_search_provenance_distinguishes_ancestors_and_focused_paths(
    search_provenance: dict[str, Path], tmp_path: Path, role: str, suffix: str, expected: str | None
) -> None:
    roots = search_provenance
    event = make_pre_tool_event(
        "mcp__cc_context__ccx_code_grep", {"text": "fixture needle", "paths": [str(roots[role] / suffix)]}
    )
    event._raw["cwd"] = str(roots["checkout"])
    result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
    assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected


def test_bash_search_uses_the_same_known_provenance_classifier(
    search_provenance: dict[str, Path], tmp_path: Path
) -> None:
    roots = search_provenance
    for path, expected in [
        (roots["collection"], "deny"),
        (roots["plugins"], "deny"),
        (roots["primary"] / "src/focused", None),
        (roots["checkout"] / ".claude", None),
        (roots["loaded"] / "fixture.py", None),
    ]:
        assert decision(f"rg needle '{path}'", roots["checkout"], tmp_path) == expected
    for command in [
        f"cd '{roots['collection']}' && rg needle .",
        f"env FIXTURE=1 rg needle '{roots['collection']}'",
        f'''sh -c "cd '{roots['collection']}' && rg needle ."''',
    ]:
        assert decision(command, roots["checkout"], tmp_path) == "deny"


def test_known_checkout_provenance_does_not_resolve_unknown_mcp_arguments(
    search_provenance: dict[str, Path], tmp_path: Path
) -> None:
    for fields in [{}, {"paths": ["."]}, {"repo": ".", "paths": ["."]}, {"paths": ["~/Code"]}]:
        event = make_pre_tool_event("mcp__cc_context__ccx_code_grep", {"text": "fixture needle"} | fields)
        event._raw["cwd"] = str(search_provenance["checkout"])
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert (result or {}).get("hookSpecificOutput", {}).get("permissionDecision") is None


def test_known_search_provenance_uses_only_metadata_and_loaded_registry(
    search_provenance: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: object, **__: object) -> NoReturn:
        raise AssertionError("provenance classification must not walk, spawn, or rediscover packs")

    monkeypatch.setattr(Target, "expand", forbidden)
    monkeypatch.setattr("captain_hook.cmd.contains_repo", forbidden)
    monkeypatch.setattr("captain_hook.cmd.glob_matches", forbidden)
    monkeypatch.setattr("captain_hook.util.vcs.scanned_names", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("captain_hook.loader.discover_pack", forbidden)
    monkeypatch.setattr("captain_hook.packs.manager.detect_languages", forbidden)
    for role in ["collection", "plugins"]:
        event = make_pre_tool_event(
            "mcp__cc_context__ccx_code_grep", {"text": "fixture needle", "paths": [str(search_provenance[role])]}
        )
        event._raw["cwd"] = str(search_provenance["checkout"])
        result = dispatch(Event.PreToolUse, event, session_dir=tmp_path / "session")
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("grep -R -n needle .", id="grep-dot"),
        pytest.param("grep -rn needle", id="grep-implicit-cwd"),
        pytest.param(r'grep -rn "CCI_RELAY_URL\s*=" infra/', id="observed-infra-search"),
        pytest.param("grep -rIn needle infra/", id="binary-skip-is-no-bound"),
        pytest.param("grep -rn --include='*' needle infra/", id="subtree-bare-include"),
        pytest.param("grep -rn --exclude-dir=vendor needle infra/", id="subtree-exclusion-only"),
        pytest.param("grep -r -e needle internal", id="regexp-role-subtree"),
        pytest.param("grep -rn -m1 needle internal", id="subtree-max-count-is-no-bound"),
        pytest.param("grep -rn needle explicit.log infra/", id="file-does-not-hide-directory"),
        pytest.param("grep -rn needle - infra/", id="stdin-does-not-hide-directory"),
        pytest.param("grep -rn --bogus-flag needle -", id="stdin-does-not-hide-incomplete-arguments"),
        pytest.param("grep -rn needle infra/*", id="subtree-universal-glob"),
        pytest.param("grep -rn needle internal/f*", id="glob-matches-directory"),
        pytest.param("grep -rn needle *.log", id="recursive-file-glob"),
        pytest.param("grep -rn needle infra/*.py", id="subtree-file-glob"),
        pytest.param("sh -c 'cd infra && grep -rn needle .'", id="nested-subtree"),
        pytest.param("rg needle .", id="rg-dot"),
        pytest.param("rg needle", id="rg-implicit-cwd"),
        pytest.param("rg --max-count 1 needle .", id="max-count-is-no-bound"),
        pytest.param("rg needle . | head -1", id="output-head-is-no-bound"),
        pytest.param("rg -d 1 needle .", id="depth-is-no-bound"),
        pytest.param("rg -g'*.py' -g'**' needle .", id="broad-glob-beside-narrow-glob"),
        pytest.param("rg -g'!*.md' needle .", id="negative-glob-only"),
        pytest.param("rg -tall needle .", id="type-all"),
        pytest.param("rg --type-add 'mine:*.py' -tmine needle .", id="type-add-voids-types"),
        pytest.param("grep -r --include='*' needle .", id="bare-include"),
        pytest.param("grep -r --exclude-dir=node_modules needle .", id="exclusion-only"),
        pytest.param("rg needle </dev/null", id="dev-null-stdin-searches-cwd"),
        pytest.param("cat patterns.txt | rg -f -", id="pattern-file-takes-stdin"),
        pytest.param("cat explicit.log | grep -r needle", id="piped-recursive-grep-searches-cwd"),
        pytest.param("cat explicit.log | ugrep -r needle", id="piped-recursive-ugrep-searches-cwd"),
        pytest.param("ugrep needle", id="ugrep-terminal-default"),
        pytest.param("ugrep -2 needle .", id="ugrep-depth-recurses"),
        pytest.param("ugrep --include-dir='*' needle .", id="ugrep-include-dir-recurses"),
        pytest.param("ugrep -r -M python needle .", id="ugrep-magic-is-no-bound"),
        pytest.param("ugrep -r -g'*.py,*' needle .", id="ugrep-broad-glob-in-list"),
        pytest.param("ugrep -r -g'*.py' --all needle .", id="ugrep-all-cancels-globs"),
        pytest.param("ugrep --include-fs -r needle .", id="ugrep-include-fs-takes-no-value"),
        pytest.param("ugrep --max-depth=2 needle .", id="ugrep-max-depth-recurses"),
        pytest.param("printf needle | rg needle </dev/null", id="redirect-replaces-pipe"),
        pytest.param("rg needle internal/focused .", id="safe-sibling-root"),
        pytest.param("rg needle internal/focused; rg needle .", id="safe-sibling-call"),
        pytest.param("rg needle *", id="bare-star-operand"),
        pytest.param("grep -r -e needle", id="regexp-role-implicit-cwd"),
        pytest.param("grep -r --file=patterns.txt .", id="file-role-root"),
        pytest.param("grep -rl -- needle .", id="end-of-options"),
        pytest.param("sudo grep -Rni needle .", id="wrapped-cluster"),
        pytest.param("rg needle $ROOT", id="unresolved-root"),
        pytest.param("rg --bogus-flag needle internal/focused", id="unknown-rg-option"),
        pytest.param("grep -rn --bogus-flag needle internal/focused", id="unknown-option-after-recursion"),
        pytest.param("git grep needle", id="git-grep-implicit-cwd"),
        pytest.param("git grep -n -e needle -- .", id="git-grep-dot-pathspec"),
        pytest.param("git grep -c needle", id="git-grep-count-is-no-config"),
        pytest.param("git --no-pager -c core.quotepath=off grep needle", id="git-grep-global-options"),
        pytest.param("git grep needle -- ':!internal'", id="git-grep-exclusion-only"),
        pytest.param("git grep needle HEAD -- .", id="git-grep-revision-is-no-bound"),
        pytest.param("git grep needle HEAD", id="git-grep-revision-without-separator"),
        pytest.param("git grep needle -- :/", id="git-grep-top-magic"),
        pytest.param("git grep needle -- '*'", id="git-grep-bare-star-pathspec"),
        pytest.param("git grep --bogus-flag needle internal/focused", id="git-grep-unknown-option"),
        pytest.param("git grep -- needle", id="git-grep-separator-before-pattern"),
        pytest.param("git grep -- needle HEAD -- .", id="git-grep-second-separator"),
        pytest.param("SPEC=':/'; git grep needle -- \"$SPEC\"", id="git-grep-expanded-top-magic"),
        pytest.param("SPEC=':!internal'; git grep needle -- \"$SPEC\"", id="git-grep-expanded-exclusion"),
        pytest.param("git grep needle -- ':(top,glob)**/**'", id="git-grep-top-universal-glob"),
        pytest.param("git --namespace -Cinternal grep needle", id="git-grep-namespace-value-is-no-hop"),
        pytest.param("git grep -nC3 needle", id="git-grep-cluster-value-implicit-cwd"),
        pytest.param("git grep -nC 3 needle -- .", id="git-grep-cluster-separate-value-dot"),
        pytest.param("git grep -nOless needle -- internal/focused", id="git-grep-optional-pager-cluster-fails-closed"),
        pytest.param("git grep needle -- internal/..", id="git-grep-parent-normalizes-to-root"),
        pytest.param("git grep needle -- internal/../.", id="git-grep-parent-dot-alias"),
        pytest.param("git grep needle -- '**/*.py/..'", id="git-grep-glob-parent-normalizes"),
    ],
)
def test_blocks_search_at_repo_root(performance: None, repo: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, repo, tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("grep -n needle explicit.log", id="not-recursive"),
        pytest.param("grep -rn needle explicit.log", id="recursive-existing-file"),
        pytest.param("grep -rn needle -", id="explicit-stdin"),
        pytest.param("grep -rn needle - explicit.log", id="stdin-and-existing-file"),
        pytest.param("grep -rIn needle explicit.log", id="binary-skip-existing-file"),
        pytest.param("grep -rn --include='*.py' needle internal/f*", id="include-bounds-directory-glob"),
        pytest.param("grep -rIn --include='*.py' needle infra/", id="subtree-narrow-include"),
        pytest.param("grep -rn --include='*.py' needle .", id="narrow-include"),
        pytest.param("grep -rn --include='*.py' --include='*.json' needle .", id="every-include-narrows"),
        pytest.param("rg -g'*.json' needle .", id="narrow-glob"),
        pytest.param("rg -tpy needle .", id="type"),
        pytest.param("rg --type=py --glob='*.json' needle .", id="type-and-glob"),
        pytest.param("rg needle internal/focused", id="deeper-subtree"),
        pytest.param("rg needle internal/*", id="glob-under-subtree"),
        pytest.param("rg needle *.py", id="named-file-glob"),
        pytest.param("cat explicit.log | rg needle", id="piped-stdin"),
        pytest.param("rg needle <explicit.log", id="redirected-stdin"),
        pytest.param("rg needle <<<'needle here'", id="here-string"),
        pytest.param("rg --files .", id="file-listing"),
        pytest.param("rg --files", id="file-listing-implicit-cwd"),
        pytest.param("rg --files -g AGENTS.md .", id="file-listing-filename-glob"),
        pytest.param("rg --files internal/focused", id="file-listing-subtree"),
        pytest.param("rg --type-list", id="type-list"),
        pytest.param("rg --help", id="help"),
        pytest.param("rg -e needle explicit.log", id="regexp-role-keeps-file"),
        pytest.param("rg -- -needle internal", id="end-of-options-pattern"),
        pytest.param("ugrep -g 'internal/*.py' needle .", id="ugrep-path-glob"),
        pytest.param("ugrep -r -tpy,json needle .", id="ugrep-type-list"),
        pytest.param("ugrep -r -Opy needle .", id="ugrep-extension"),
        pytest.param("ugrep needle .", id="ugrep-skips-directories"),
        pytest.param("cat explicit.log | ugrep needle", id="ugrep-piped-stdin"),
        pytest.param("rg '/' internal", id="path-like-pattern"),
        pytest.param("echo 'rg needle .'", id="quoted-command"),
        pytest.param("git grep needle internal/focused", id="git-grep-subtree"),
        pytest.param("git grep -c needle internal", id="git-grep-count-subtree"),
        pytest.param("git grep needle explicit.log", id="git-grep-named-file"),
        pytest.param("git grep needle HEAD -- internal", id="git-grep-revision-and-subtree"),
        pytest.param("git grep needle -- '*.py'", id="git-grep-glob-pathspec"),
        pytest.param("git grep -e needle -- ':(glob)**/*.py'", id="git-grep-long-magic-glob"),
        pytest.param("git grep needle -- :/internal", id="git-grep-top-relative-subtree"),
        pytest.param("git grep -- needle internal", id="git-grep-separator-before-pattern-subtree"),
        pytest.param('SPEC=internal; git grep needle -- "$SPEC"', id="git-grep-expanded-subtree"),
        pytest.param("git grep -nC3 needle -- internal/focused", id="git-grep-cluster-value-subtree"),
        pytest.param("git grep -nC 3 needle -- internal/focused", id="git-grep-cluster-separate-value-subtree"),
        pytest.param("git grep needle -- internal/focused/../focused", id="git-grep-parent-back-into-subtree"),
        pytest.param("git grep -e needle --and \\( -e other \\) internal", id="git-grep-expression-groups"),
        pytest.param("git grep --help", id="git-grep-help"),
        pytest.param("git grep", id="git-grep-without-pattern"),
        pytest.param("echo 'git grep needle'", id="quoted-git-grep"),
    ],
)
def test_allows_bounded_search_at_repo_root(
    performance: None, repo: Path, home: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command, repo, tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("ugrep -rn needle ~/.claude/scratch/release-v3", id="scratch-project"),
        pytest.param("ugrep -rl needle ~/.claude/scratch/release-v3", id="scratch-project-listing"),
        pytest.param("sh -c 'cd ~/.claude/scratch/release-v3 && ugrep -rn needle .'", id="cd-in-payload"),
        pytest.param("cd ~/.claude/scratch/release-v3 && grep -rn needle .", id="cd-before-call"),
        pytest.param(
            'cd ~/.claude/scratch/durable-agents-rebuild && grep -rl "backup in cloud storage" . '
            "2>/dev/null | grep -v node_modules | head -3",
            id="incident-recursive-scratch-pipeline",
        ),
        pytest.param("rg needle ~/.claude/worktrees/captain-hook", id="worktree-pool"),
        pytest.param("rg needle ~/.claude/worktrees/captain-hook/lane", id="worktree-root"),
        pytest.param("rg needle ~", id="home"),
        pytest.param('rg needle "$HOME"', id="home-variable"),
    ],
)
def test_blocks_search_at_home_roots(performance: None, home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("ugrep -rn needle ~/.claude/scratch/release-v3/specific-lane", id="scratch-lane"),
        pytest.param("rg needle ~/.claude/worktrees/captain-hook/lane/src", id="worktree-subtree"),
        pytest.param("rg needle '~'", id="quoted-tilde"),
        pytest.param("rg needle ~/.claude/scratch/notes.log", id="file-under-scratch-root"),
    ],
)
def test_allows_search_below_home_roots(performance: None, home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "rg --files -g AGENTS.md -g CLAUDE.md ~/.claude/worktrees/captain-hook/lane "
            "~/.claude/worktrees/captain-hook ~/.claude/worktrees",
            id="instruction-listing-over-checkout-and-pools",
        ),
        pytest.param("rg --files ~/.claude/worktrees/captain-hook", id="worktree-pool"),
        pytest.param("rg --files -g AGENTS.md ~/.claude/worktrees/captain-hook", id="worktree-pool-is-no-pattern"),
        pytest.param("rg --files ~/.claude/worktrees", id="whole-pool"),
        pytest.param("rg --files -g AGENTS.md ~/.claude/worktrees", id="whole-pool-filename-glob"),
        pytest.param("rg --files --glob=CLAUDE.md ~/.claude/worktrees", id="whole-pool-joined-long-glob"),
        pytest.param("rg --files -gAGENTS.md ~/.claude/worktrees", id="whole-pool-joined-short-glob"),
        pytest.param("rg --files -g AGENTS.md ~/.orca/workspaces/captain-hook", id="orca-workspace-pool"),
        pytest.param("rg --files ~", id="home"),
        pytest.param("rg --files -g '*.py' ~", id="home-filename-glob"),
        pytest.param("rg --files -tpy ~", id="home-type-filter"),
        pytest.param('rg --files "$HOME"', id="home-variable"),
        pytest.param("cd ~ && rg --files", id="implicit-cwd-home"),
        pytest.param(
            "rg --files ~/.claude/worktrees/captain-hook/lane/src ~/.claude/worktrees", id="safe-sibling-beside-pool"
        ),
        pytest.param("rg --type-list --files ~", id="later-files-mode"),
        pytest.param("rg --generate=man --files ~", id="files-after-generate"),
        pytest.param("rg --generate man --files ~", id="files-after-separate-generate"),
        pytest.param("rg --files --type-list --files ~", id="last-of-repeated-modes"),
        pytest.param("rg --files --type-list -nC3 --files ~", id="files-hidden-past-incomplete-binding"),
        pytest.param("rg --files ~ --type-list -nC3 --files", id="root-before-incomplete-binding"),
        pytest.param('rg --files -g AGENTS.md "$POOL"', id="unresolved-root"),
        pytest.param("rg --files /", id="filesystem-root"),
        pytest.param("rg --files {home.parent}", id="home-parent"),
        pytest.param("rg --files ~/..", id="tilde-parent"),
        pytest.param("rg --files ~/.claude", id="claude-dir"),
        pytest.param("rg --files -g AGENTS.md ~/.claude", id="claude-dir-filename-glob"),
        pytest.param("rg --files ~/.orca", id="orca-dir"),
        pytest.param("cd ~/.claude && rg --files", id="implicit-cwd-claude-dir"),
        pytest.param("rg --files ~/.claude/worktrees/README ~/.claude/worktrees", id="explicit-file-beside-pool"),
        pytest.param(
            "rg --files -g AGENTS.md ~/.claude/worktrees/README ~/.claude/worktrees/captain-hook",
            id="explicit-file-beside-repo-aggregate",
        ),
        pytest.param("rg --files ~/project/.claude/worktrees", id="project-local-pool"),
        pytest.param("rg --files -g AGENTS.md ~/project/.claude/worktrees", id="project-local-pool-filename-glob"),
        pytest.param("rg --files ~/project/.claude/worktrees/stale", id="project-local-unmarked-child"),
        pytest.param("cd ~/project/.claude/worktrees && rg --files", id="implicit-cwd-project-local-pool"),
        pytest.param("rg --files ~/repos/orca/workspaces", id="adopted-pool"),
        pytest.param("rg --files -g AGENTS.md ~/repos/orca/workspaces", id="adopted-pool-filename-glob"),
        pytest.param("rg --files ~/repos/orca/workspaces/{uuid}", id="adopted-uuid-aggregate"),
        pytest.param("rg --files -tmd ~/repos/orca/workspaces/{uuid}", id="adopted-uuid-type-filter"),
        pytest.param("rg --files ~/repos/orca/workspaces/{uuid}/captain-hook", id="adopted-repo-aggregate"),
        pytest.param(
            "rg --files -g AGENTS.md -g CLAUDE.md ~/repos/orca/workspaces/{uuid}/captain-hook",
            id="adopted-repo-aggregate-filename-glob",
        ),
        pytest.param("cd ~/repos/orca/workspaces/{uuid} && rg --files", id="implicit-cwd-adopted-uuid"),
        pytest.param(
            "rg --files ~/repos/orca/workspaces/{uuid}/captain-hook/lane ~/repos/orca/workspaces/{uuid}/captain-hook",
            id="adopted-checkout-beside-repo-aggregate",
        ),
        pytest.param("rg --files ~/repos/orca", id="adopted-pool-parent"),
        pytest.param("rg --files ~/repos", id="adopted-pool-grandparent"),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup/jobs", id="cleanup-queue"),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup/jobs/queued-job", id="cleanup-job-root"),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup", id="cleanup-queue-parent"),
        pytest.param(
            "rg --files {home}/.daemonkit/a/ccx-cleanup -g '*.json' -g '*.toml'",
            id="observed-cleanup-queue-parent-with-filename-filters",
        ),
        pytest.param("rg --files ~/.daemonkit/a", id="cleanup-queue-grandparent"),
        pytest.param("rg --files ~/.daemonkit", id="cleanup-queue-ancestor"),
        pytest.param("cd ~/.daemonkit/a/ccx-cleanup && rg --files", id="implicit-cleanup-queue-parent"),
        pytest.param("rg --files --maxdepth 1 ~/.daemonkit/a/ccx-cleanup", id="cleanup-retains-listing-depth-policy"),
    ],
)
def test_blocks_listing_at_home_roots(performance: None, home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command.format(home=home, uuid=ORCA_UUID), tmp_path.resolve(), tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rg --files ~/.claude/worktrees/captain-hook/lane", id="single-checkout-in-pool"),
        pytest.param(
            "rg --files -g AGENTS.md -g CLAUDE.md ~/.claude/worktrees/captain-hook/lane",
            id="instruction-listing-single-checkout",
        ),
        pytest.param("rg --files ~/.claude/worktrees/captain-hook/lane/src", id="checkout-subtree"),
        pytest.param("rg --files ~/.claude/scratch", id="scratch-root"),
        pytest.param("rg --files ~/.claude/scratch/release-v3", id="scratch-project"),
        pytest.param("rg --files ~/.config", id="home-child-without-pool"),
        pytest.param("rg --files /tmp/scratch-lane", id="temp-lane"),
        pytest.param("rg --files '~'", id="quoted-tilde"),
        pytest.param("rg --files --help ~", id="help"),
        pytest.param("rg --files --version ~", id="version"),
        pytest.param("rg --files --type-list ~", id="later-type-list-mode"),
        pytest.param("rg --files --generate=man ~", id="later-generate-mode"),
        pytest.param("rg --files --generate man ~", id="later-separate-generate-mode"),
        pytest.param("rg --files ~/.claude/worktrees/README", id="explicit-file-in-pool"),
        pytest.param('rg --files "$HOME/.claude/worktrees/README"', id="explicit-file-in-pool-home-variable"),
        pytest.param("rg --files ~/project/.claude/worktrees/lane", id="project-local-checkout"),
        pytest.param(
            "rg --files -g AGENTS.md ~/project/.claude/worktrees/lane", id="project-local-checkout-filename-glob"
        ),
        pytest.param("rg --files ~/project/.claude/worktrees/lane/src", id="project-local-checkout-subtree"),
        pytest.param("cd ~/project/.claude/worktrees/lane && rg --files", id="implicit-cwd-project-local-checkout"),
        pytest.param("rg --files ~/repos/orca/workspaces/{uuid}/captain-hook/lane", id="adopted-checkout"),
        pytest.param(
            "rg --files -g AGENTS.md ~/repos/orca/workspaces/{uuid}/captain-hook/lane/src",
            id="adopted-checkout-subtree",
        ),
        pytest.param(
            "cd ~/repos/orca/workspaces/{uuid}/captain-hook/lane && rg --files", id="implicit-cwd-adopted-checkout"
        ),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup/switch.json", id="cleanup-switch-file"),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup/jobs/queued-job/job.json", id="cleanup-job-file"),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup/jobs/queued-job/payload", id="cleanup-one-checkout"),
        pytest.param("rg --files ~/.daemonkit/a/ccx-cleanup/jobs/queued-job/payload/src", id="cleanup-checkout-subtree"),
        pytest.param("rg paused ~/.daemonkit/a/ccx-cleanup/switch.json", id="cleanup-direct-state-search"),
        pytest.param("cat ~/.daemonkit/a/ccx-cleanup/switch.json", id="cleanup-direct-state-read"),
        pytest.param("find ~/.daemonkit/a/ccx-cleanup -maxdepth 1 -name '*.json'", id="cleanup-shallow-find"),
    ],
)
def test_allows_listing_below_home_roots(performance: None, home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command.format(uuid=ORCA_UUID), tmp_path.resolve(), tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rg --files ~/repos/orca/workspaces/{linked}", id="linked-uuid-aggregate"),
        pytest.param(
            "rg --files --follow -g AGENTS.md ~/repos/orca/workspaces/{linked}", id="followed-linked-uuid-aggregate"
        ),
        pytest.param("rg --files ~/repos/orca/workspaces/{uuid}/linked-repo", id="linked-repo-aggregate"),
    ],
)
def test_blocks_listing_at_a_linked_adopted_aggregate(
    performance: None, linked_aggregates: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(linked=LINKED_UUID, uuid=ORCA_UUID), tmp_path.resolve(), tmp_path) == "deny"


def test_allows_a_linked_checkout_in_the_adopted_pool(
    performance: None, linked_aggregates: Path, tmp_path: Path
) -> None:
    assert (
        decision(f"rg --files ~/repos/orca/workspaces/{ORCA_UUID}/linked-checkout", tmp_path.resolve(), tmp_path)
        is None
    )


def test_listing_follows_the_working_directory(performance: None, home: Path, tmp_path: Path) -> None:
    pool = home / ".claude" / "worktrees"
    assert decision("rg --files", home, tmp_path / "home") == "deny"
    assert decision("rg --files -g AGENTS.md", pool, tmp_path / "pool") == "deny"
    assert decision("rg --files -g AGENTS.md .", pool / "captain-hook", tmp_path / "repo-pool") == "deny"
    assert decision("rg --files -g AGENTS.md", pool / "captain-hook" / "lane", tmp_path / "checkout") is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("sh -c 'cd ~ && git grep needle'", id="nested-cd-home-git-grep"),
        pytest.param("cd ~ && git grep needle", id="cd-home-git-grep"),
        pytest.param("git -C ~ grep needle", id="git-c-home"),
        pytest.param("cd ~ && rg needle .", id="cd-home-rg"),
        pytest.param("sh -c 'cd ~ && grep -rn needle .'", id="nested-cd-home-grep"),
    ],
)
def test_blocks_search_at_a_symlinked_home(performance: None, linked_home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("sh -c 'cd ~/project && git grep needle'", id="nested-cd-home-subdirectory-git-grep"),
        pytest.param("cd ~/project && rg needle .", id="cd-home-subdirectory-rg"),
    ],
)
def test_allows_search_below_a_symlinked_home(
    performance: None, linked_home: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rg --files -g AGENTS.md ~", id="tilde"),
        pytest.param("cd ~ && rg --files", id="cd-home-implicit-cwd"),
        pytest.param("rg --files {real}", id="canonical-path"),
        pytest.param("rg --files ~/..", id="tilde-parent"),
        pytest.param("rg --files {real.parent}", id="canonical-parent"),
        pytest.param("rg --files ~/.claude", id="tilde-claude-dir"),
        pytest.param("rg --files {real}/repos/orca/workspaces", id="canonical-adopted-pool"),
        pytest.param("rg --files {real}/repos", id="canonical-adopted-pool-ancestor"),
    ],
)
def test_blocks_listing_at_a_symlinked_home(performance: None, linked_home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command.format(real=linked_home.resolve()), tmp_path.resolve(), tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rg --files ~/project", id="tilde-subdirectory"),
        pytest.param("cd ~/project && rg --files", id="cd-home-subdirectory"),
        pytest.param("rg --files {real}/project", id="canonical-subdirectory"),
    ],
)
def test_allows_listing_below_a_symlinked_home(
    performance: None, linked_home: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(real=linked_home.resolve()), tmp_path.resolve(), tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rg --files {home.parent}", id="lexical-parent"),
        pytest.param("rg --files --hidden --follow {home.parent}", id="lexical-parent-followed"),
        pytest.param("rg --files -g AGENTS.md {home.parent}", id="lexical-parent-filename-glob"),
        pytest.param("rg --files {real.parent}", id="resolved-parent"),
        pytest.param("rg --files ~/..", id="tilde-parent"),
    ],
)
def test_blocks_listing_above_a_relocated_home(
    performance: None, relocated_home: Path, tmp_path: Path, command: str
) -> None:
    listing = command.format(home=relocated_home, real=relocated_home.resolve())
    assert decision(listing, tmp_path.resolve(), tmp_path) == "deny"


def test_allows_listing_below_a_relocated_home(performance: None, relocated_home: Path, tmp_path: Path) -> None:
    assert decision("rg --files ~/project", tmp_path.resolve(), tmp_path) is None
    assert decision("cd ~/project && rg --files", tmp_path.resolve(), tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("rg --files ~/.claude", id="lexical-parent"),
        pytest.param("rg --files -g AGENTS.md ~/.claude", id="lexical-parent-filename-glob"),
        pytest.param("cd ~/.claude && rg --files", id="implicit-cwd-lexical-parent"),
        pytest.param("rg --files ~/.claude/worktrees", id="linked-pool"),
        pytest.param("rg --files ~/.claude/worktrees/captain-hook", id="linked-pool-repo-aggregate"),
        pytest.param("rg --files {pool.parent}", id="resolved-parent"),
    ],
)
def test_blocks_listing_above_a_relocated_pool(
    performance: None, relocated_pool: Path, tmp_path: Path, command: str
) -> None:
    listing = command.format(pool=(relocated_pool / ".claude" / "worktrees").resolve())
    assert decision(listing, tmp_path.resolve(), tmp_path) == "deny"


def test_allows_a_checkout_in_a_relocated_pool(performance: None, relocated_pool: Path, tmp_path: Path) -> None:
    assert decision("rg --files ~/.claude/worktrees/captain-hook/lane", tmp_path.resolve(), tmp_path) is None


def test_match_supplies_the_ugrep_pattern(performance: None, repo: Path, tmp_path: Path) -> None:
    assert decision("ugrep -r --match /", repo / "internal" / "focused", tmp_path) == "deny"
    assert decision("ugrep -r --match .", repo / "internal" / "focused", tmp_path) is None


def test_classifies_roots_without_walking(
    performance: None, repo: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def walk(*_: object, **__: object) -> NoReturn:
        raise AssertionError("the search guard walked a tree")

    monkeypatch.setattr(Target, "expand", walk)
    monkeypatch.setattr("captain_hook.cmd.contains_repo", walk)
    monkeypatch.setattr("captain_hook.cmd.glob_matches", walk)
    monkeypatch.setattr("captain_hook.util.vcs.scanned_names", walk)
    assert decision("rg needle .", repo, tmp_path / "dot") == "deny"
    assert decision("grep -rn needle *", repo, tmp_path / "star") == "deny"
    assert decision("grep -rn needle infra/", repo, tmp_path / "infra") == "deny"
    assert decision("grep -rn needle explicit.log", repo, tmp_path / "file") is None
    assert decision("grep -rn needle -", repo, tmp_path / "stdin") is None
    assert decision("grep -rn needle - infra/", repo, tmp_path / "stdin-directory") == "deny"
    assert decision("grep -rn needle - explicit.log", repo, tmp_path / "stdin-file") is None
    assert decision("grep -rn needle infra/*.py", repo, tmp_path / "glob") == "deny"
    assert decision("grep -rn needle internal/f*", repo, tmp_path / "directory-glob") == "deny"
    assert decision("grep -rn --include='*.py' needle internal/f*", repo, tmp_path / "bounded-glob") is None
    assert decision("rg needle internal/*", repo, tmp_path) is None
    assert decision("rg needle internal/focused", repo, tmp_path) is None
    assert decision("git grep needle", repo, tmp_path / "git") == "deny"
    assert decision("git grep needle internal/focused", repo, tmp_path) is None
    assert decision("rg --files -g AGENTS.md . internal/focused", repo, tmp_path) is None
    assert decision("rg --files ~", repo, tmp_path / "home") == "deny"
    assert (
        decision(
            f"rg --files {home}/.daemonkit/a/ccx-cleanup -g '*.json' -g '*.toml'", repo, tmp_path / "cleanup"
        )
        == "deny"
    )
    assert decision("rg --files ~/.daemonkit/a/ccx-cleanup/switch.json", repo, tmp_path / "switch") is None


def test_git_grep_follows_its_own_directory(performance: None, repo: Path, tmp_path: Path) -> None:
    focused = repo / "internal" / "focused"
    assert decision("git grep needle", focused, tmp_path / "subtree") is None
    assert decision("git grep needle -- :/", focused, tmp_path / "top") == "deny"
    assert decision(f"git -C {repo} grep needle", focused, tmp_path / "absolute") == "deny"
    assert decision("git -C ../.. grep needle", focused, tmp_path / "relative") == "deny"
    assert decision("git -C ../.. -C internal grep needle", focused, tmp_path / "chained") is None
    assert decision(f"git -C {repo}/internal grep needle", tmp_path, tmp_path / "outside") is None
    assert decision("git grep needle -- ../..", focused, tmp_path / "parents") == "deny"
    assert decision("git grep needle -- ../focused", focused, tmp_path / "sibling") is None


def test_explicit_git_dir_searches_the_whole_index(indexed: Path) -> None:
    def hits(*pathspecs: str, git_dir: Path | None = None) -> list[str]:
        location = () if git_dir is None else (f"--git-dir={git_dir}",)
        return subprocess.run(
            ["git", *location, "grep", "--cached", "-l", "needle", "--", *pathspecs],
            cwd=indexed,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

    git_dir = indexed.parents[1] / ".git"
    assert hits() == ["inside.txt"]
    assert "outside.txt" in hits(git_dir=git_dir)
    assert "outside.txt" in hits("internal/..", git_dir=git_dir)
    assert "outside.txt" in hits("internal/../.", git_dir=git_dir)
    assert hits("internal/focused", git_dir=git_dir) == ["internal/focused/inside.txt"]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git --git-dir={root}/.git grep --cached needle", id="git-dir-option"),
        pytest.param("git --git-dir {root}/.git grep needle -- .", id="git-dir-separate-value"),
        pytest.param("GIT_DIR={root}/.git git grep --cached needle", id="git-dir-variable"),
        pytest.param("env GIT_DIR={root}/.git git grep --cached needle", id="git-dir-env-wrapper"),
        pytest.param("export GIT_DIR={root}/.git; git grep --cached needle", id="git-dir-exported"),
        pytest.param("git --git-dir={root}/.git grep --cached needle -- :/", id="git-dir-top-magic"),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/..", id="git-dir-parent-normalizes-to-top"
        ),
        pytest.param("git --git-dir={root}/.git grep --cached needle -- internal/../.", id="git-dir-parent-dot-alias"),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/focused/../..", id="git-dir-double-parent"
        ),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- ':(glob)internal/..'", id="git-dir-magic-parent"
        ),
        pytest.param("env 'GIT_DIR={root}/.git' git grep --cached needle", id="git-dir-quoted-env-wrapper"),
        pytest.param("GIT_DIR={root}/.git sh -c 'git grep --cached needle'", id="git-dir-inherited-by-payload"),
        pytest.param(
            'GIT_DIR={root}/.git sh -c "git grep --cached needle"', id="git-dir-inherited-by-double-quoted-payload"
        ),
        pytest.param("env GIT_DIR={root}/.git sh -c 'git grep --cached needle'", id="git-dir-inherited-through-env"),
        pytest.param("export GIT_DIR={root}/.git; sh -c 'git grep --cached needle'", id="git-dir-exported-to-payload"),
    ],
)
def test_blocks_git_grep_from_a_subtree_with_an_explicit_git_dir(
    performance: None, indexed: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(root=indexed.parents[1]), indexed, tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git --git-dir={root}/.git --work-tree={root} grep needle", id="work-tree-option"),
        pytest.param("GIT_WORK_TREE={root} git --git-dir={root}/.git grep needle", id="work-tree-variable"),
    ],
)
def test_blocks_git_grep_over_an_explicit_work_tree(
    performance: None, indexed: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(root=indexed.parents[1]), tmp_path, tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git grep --cached needle", id="discovered-repository-keeps-its-prefix"),
        pytest.param("git -C {root}/internal/focused grep --cached needle", id="plain-c-keeps-its-scope"),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/focused", id="git-dir-narrowing-pathspec"
        ),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/focused/../focused",
            id="git-dir-parent-back-into-subtree",
        ),
        pytest.param("GIT_DIR={root}/.git; sh -c 'git grep --cached needle'", id="unexported-assignment-stays-local"),
    ],
)
def test_allows_scoped_git_grep_in_a_real_repository(
    performance: None, indexed: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(root=indexed.parents[1]), indexed, tmp_path) is None


def test_search_denial_names_its_bounds(performance: None, repo: Path, tmp_path: Path) -> None:
    result = dispatch(Event.PreToolUse, evt_for("rg needle .", cwd=str(repo)), session_dir=tmp_path / "session")
    assert result is not None
    assert "`-g '*.py'`" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_blocks_successive_searches_in_one_session(performance: None, repo: Path, tmp_path: Path) -> None:
    assert decision("grep -rl needle .", repo, tmp_path) == "deny"
    assert decision("rg other .", repo, tmp_path) == "deny"
    assert decision("rg needle internal/focused", repo, tmp_path) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(r'grep -rn "CCI_RELAY_URL\s*=" infra/', "deny", id="observed-argv"),
        pytest.param("grep -rIn needle infra/", "deny", id="binary-skip"),
        pytest.param("grep -rn --include='*' needle infra/", "deny", id="universal-include"),
        pytest.param("grep -rn needle internal/f*", "deny", id="directory-glob"),
        pytest.param("grep -rn --include='*.py' needle infra/", None, id="narrow-include"),
        pytest.param("grep -rn --include='*.py' needle internal/f*", None, id="include-bound-glob"),
        pytest.param("grep -rn needle explicit.log", None, id="named-file"),
        pytest.param("grep -rn needle -", None, id="explicit-stdin"),
        pytest.param("grep -rn needle - infra/", "deny", id="stdin-directory"),
        pytest.param("grep -rn needle - explicit.log", None, id="stdin-file"),
    ],
)
def test_recursive_grep_cli_requires_a_filename_bound(repo: Path, command: str, expected: str | None) -> None:
    result = run_cli(
        "run",
        "PreToolUse",
        root_dir=str(repo),
        stdin_data=json.dumps(
            {
                "session_id": "recursive-grep",
                "cwd": str(repo),
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
        ),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout) if result.stdout.strip() else {}
    decision = output.get("hookSpecificOutput", {})
    assert decision.get("permissionDecision") == expected
    if expected == "deny":
        assert "A recursive `grep` over a directory" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(r'grep -rn "CCI_RELAY_URL\s*=" infra/', "deny", id="directory"),
        pytest.param("grep -rIn needle infra/", "deny", id="binary-skip"),
        pytest.param("grep -rn needle internal/f*", "deny", id="directory-glob"),
        pytest.param("grep -rn --include='*.py' needle infra/", None, id="narrow-include"),
        pytest.param("grep -rn --include='*.py' needle internal/f*", None, id="include-bound-glob"),
        pytest.param("grep -rn needle explicit.log", None, id="named-file"),
        pytest.param("grep -rn needle -", None, id="explicit-stdin"),
        pytest.param("grep -rn needle - infra/", "deny", id="stdin-directory"),
        pytest.param("grep -rn needle - explicit.log", None, id="stdin-file"),
    ],
)
def test_recursive_grep_uses_the_current_command_without_history(
    performance: None, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, expected: str | None
) -> None:
    from captain_hook.cli import dispatch_event

    monkeypatch.setattr(
        "captain_hook.transcripts.registered_sources", lambda _: pytest.fail("registered history was read")
    )
    output, _ = dispatch_event(
        repo,
        Event.PreToolUse,
        {
            "session_id": "recursive-grep",
            "cwd": str(repo),
            "transcript_path": str(tmp_path / "unread-history.jsonl"),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
        session_dir=tmp_path / "session",
        transcript_loader=lambda _: pytest.fail("history loader was called"),
    )
    assert (output or {}).get("hookSpecificOutput", {}).get("permissionDecision") == expected
