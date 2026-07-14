from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from captain_hook.cli import DEFAULT_PREFIX, LintResult, lint_pack
from captain_hook.packs import manager
from tests.helpers import run_cli

pytestmark = pytest.mark.usefixtures("isolate_modules")

CANONICAL_ATTACH = f'{DEFAULT_PREFIX} pack attach "${{CLAUDE_PLUGIN_ROOT}}"'
CAPTAIN_DEP = [{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=9.8.0"}]
POST_TOOL_HOOK = 'from captain_hook import Event, hook\n\nhook(Event.PostToolUse, message="m")\n'


# --- fixture builders ----------------------------------------------------------------


def write_manifest(root: Path, *, name: str = "ccx", hooks: str = ".") -> None:
    root.mkdir(parents=True, exist_ok=True)
    body = f'name = "{name}"\nversion = "0.1.0"\ndescription = "d"\nhooks = "{hooks}"\n'
    (root / manager.PACK_MANIFEST).write_text(body)


def write_hook(root: Path, src: str = POST_TOOL_HOOK) -> None:
    (root / "h.py").write_text(src)


def write_hooks_json(root: Path, data: dict[str, Any]) -> None:
    (hooks_dir := root / "hooks").mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(json.dumps(data))


def attach_only(command: str = CANONICAL_ATTACH) -> dict[str, Any]:
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


def write_plugin_json(root: Path, deps: list[Any]) -> None:
    (d := root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"name": "ccx", "dependencies": deps}))


def write_marketplace(root: Path, allow: list[str]) -> None:
    (d := root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / "marketplace.json").write_text(json.dumps({"name": "ccx", "allowCrossMarketplaceDependenciesOn": allow}))


def conforming(root: Path) -> Path:
    """A pack that satisfies every contract check."""
    write_manifest(root)
    write_hook(root)
    write_hooks_json(root, attach_only())
    write_plugin_json(root, CAPTAIN_DEP)
    write_marketplace(root, ["captain-hook"])
    return root


def by_check(root: Path) -> dict[str, LintResult]:
    return {r.check: r for r in lint_pack(root)}


def failed(results: dict[str, LintResult]) -> list[str]:
    return [r.check for r in results.values() if not r.ok and not r.warning]


# --- the happy path ------------------------------------------------------------------


def test_conforming_pack_passes_every_check(tmp_path: Path) -> None:
    results = by_check(conforming(tmp_path / "ccx"))
    assert failed(results) == []
    assert not results["marketplace.json"].warning  # a present-and-correct allowlist is a pass, not a warning
    assert {"manifest", "hooks.json", "plugin.json", "marketplace.json", "load", "session-start", "async-decision"} == (
        set(results)
    )


def test_conforming_pack_exits_zero(tmp_path: Path) -> None:
    result = run_cli("pack", "lint", str(conforming(tmp_path / "ccx")))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout


# --- the legacy getaway shape (must fail) --------------------------------------------


def legacy_getaway_hooks_json() -> dict[str, Any]:
    # getaway's current shape: a bare-uvx attach (missing --isolated) plus mirrored run entries
    # that the sole-dispatcher contract forbids.
    def cmd(command: str) -> dict[str, Any]:
        return {"type": "command", "command": command}

    return {
        "hooks": {
            "SessionStart": [{"hooks": [cmd('uvx capt-hook pack attach "${CLAUDE_PLUGIN_ROOT}"')]}],
            "PostToolUse": [{"matcher": "Skill", "hooks": [cmd("uvx capt-hook run PostToolUse")]}],
            "Stop": [{"hooks": [cmd("uvx capt-hook run Stop")]}],
        }
    }


def test_legacy_getaway_shape_fails(tmp_path: Path) -> None:
    root = tmp_path / "getaway"
    write_manifest(root, name="getaway")
    write_hook(root)
    write_hooks_json(root, legacy_getaway_hooks_json())
    write_plugin_json(root, [])  # getaway declares no captain-hook dependency today
    # no marketplace.json on disk

    results = by_check(root)
    assert not results["hooks.json"].ok  # the run entries are the whole point of the migration
    assert "run" in results["hooks.json"].reason
    assert not results["plugin.json"].ok  # missing captain-hook dependency
    assert results["marketplace.json"].warning  # absent marketplace.json is a warning, not a hard failure
    assert set(failed(results)) >= {"hooks.json", "plugin.json"}


def test_legacy_getaway_shape_exits_nonzero(tmp_path: Path) -> None:
    root = tmp_path / "getaway"
    write_manifest(root, name="getaway")
    write_hook(root)
    write_hooks_json(root, legacy_getaway_hooks_json())
    write_plugin_json(root, [])

    result = run_cli("pack", "lint", str(root))
    assert result.returncode == 1
    assert "FAIL" in result.stdout


# --- per-check coverage --------------------------------------------------------------


def test_missing_manifest_fails_and_skips_hook_load(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    results = by_check(root)
    assert not results["manifest"].ok
    assert not results["load"].ok  # no manifest, no hooks loaded
    assert not results["session-start"].ok  # can't load hooks without a manifest
    assert "not loaded" in results["async-decision"].reason


def test_pack_with_no_hooks_fails_load(tmp_path: Path) -> None:
    # A valid manifest but an empty hooks dir ships no working guard: the load check must fail.
    root = conforming(tmp_path / "ccx")
    (root / "h.py").unlink()
    result = by_check(root)["load"]
    assert not result.ok
    assert "no hooks loaded" in result.reason
    assert run_cli("pack", "lint", str(root)).returncode == 1


def test_syntax_broken_hook_file_fails_load(tmp_path: Path) -> None:
    # A hook file that won't import lands in load_errors — a non-async load error the load check
    # must surface (before this fix only AsyncDecisionError was inspected).
    root = conforming(tmp_path / "ccx")
    (root / "h.py").write_text("def broken(:\n")  # syntax error
    result = by_check(root)["load"]
    assert not result.ok
    assert "failed to load" in result.reason
    assert run_cli("pack", "lint", str(root)).returncode == 1


def test_nonexistent_hooks_dir_fails_load(tmp_path: Path) -> None:
    # The manifest points hooks at a dir that isn't there: discovery loads nothing, load fails.
    root = tmp_path / "ccx"
    write_manifest(root, hooks="nope")
    write_hooks_json(root, attach_only())
    write_plugin_json(root, CAPTAIN_DEP)
    write_marketplace(root, ["captain-hook"])
    assert not by_check(root)["load"].ok
    assert run_cli("pack", "lint", str(root)).returncode == 1


def test_run_entry_alone_fails_hooks_json(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(
        root,
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": CANONICAL_ATTACH}]}],
                "Stop": [{"hooks": [{"type": "command", "command": f"{DEFAULT_PREFIX} run Stop"}]}],
            }
        },
    )
    assert not by_check(root)["hooks.json"].ok  # even the canonical run prefix is forbidden in a pack


def test_non_capt_hook_command_entries_are_ignored(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    data = attach_only()
    data["hooks"]["SessionStart"][0]["hooks"].insert(
        0, {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/install-binary.sh"}
    )
    write_hooks_json(root, data)
    assert by_check(root)["hooks.json"].ok  # a plugin's own shell hook is permitted alongside the attach


def test_attach_not_under_session_start_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": CANONICAL_ATTACH}]}]}})
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "SessionStart" in result.reason


def test_missing_hooks_json_fails(tmp_path: Path) -> None:
    root = tmp_path / "ccx"
    write_manifest(root)
    write_hook(root)
    write_plugin_json(root, CAPTAIN_DEP)
    assert not by_check(root)["hooks.json"].ok


def test_plugin_json_without_dependency_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "something-else"}])
    assert not by_check(root)["plugin.json"].ok


def test_plugin_json_name_only_dependency_fails(tmp_path: Path) -> None:
    # A name-only entry references captain-hook but lacks the marketplace + version floor the
    # cross-marketplace contract needs; the reason names what to add.
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook"}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "marketplace" in result.reason and "version" in result.reason


def test_plugin_json_string_form_dependency_fails(tmp_path: Path) -> None:
    # A bare-string dependency can't carry a marketplace or version floor, so it fails.
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, ["captain-hook"])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "version" in result.reason


def test_missing_marketplace_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "marketplace.json").unlink()
    result = by_check(root)["marketplace.json"]
    assert result.ok and result.warning  # reported, but does not fail the lint
    assert failed(by_check(root)) == []


def test_marketplace_without_allowlist_entry_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_marketplace(root, ["some-other-plugin"])
    result = by_check(root)["marketplace.json"]
    assert not result.ok and not result.warning


def test_pack_subscribing_session_start_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hook(root, 'from captain_hook import Event, hook\n\nhook(Event.SessionStart, message="racy")\n')
    result = by_check(root)["session-start"]
    assert not result.ok
    assert "SessionStart" in result.reason


def test_async_hook_on_decision_event_fails(tmp_path: Path) -> None:
    # The registration guard raises on import, so the module lands in load_errors — lint surfaces it.
    root = conforming(tmp_path / "ccx")
    write_hook(root, 'from captain_hook import Event, hook\n\nhook(Event.Stop, message="bg", async_=True)\n')
    result = by_check(root)["async-decision"]
    assert not result.ok
    assert "discarded" in result.reason


def test_nested_manifest_layout_resolves_sibling_hooks_json(tmp_path: Path) -> None:
    # When the manifest lives in the plugin's hooks/ dir, the attach line passes that dir and
    # hooks.json is its sibling; plugin.json/marketplace.json resolve by searching upward.
    plugin = tmp_path / "plugin"
    hooks = plugin / "hooks"
    write_manifest(hooks, hooks=".")
    (hooks / "h.py").write_text(POST_TOOL_HOOK)
    (hooks / "hooks.json").write_text(json.dumps(attach_only()))
    write_plugin_json(plugin, CAPTAIN_DEP)
    write_marketplace(plugin, ["captain-hook"])

    assert failed(by_check(hooks)) == []  # lint receives the hooks/ dir, matching the attach line


# --- fix #2: conservative rejection of any non-canonical capt-hook usage ---------------


def _with_extra_entry(root: Path, command: str) -> None:
    data = attach_only()
    data["hooks"].setdefault("PostToolUse", []).append({"hooks": [{"type": "command", "command": command}]})
    write_hooks_json(root, data)


def test_bash_embedded_capt_hook_run_fails(tmp_path: Path) -> None:
    # capt-hook buried inside a `bash -c '…'` string is not a top-level argv token, but the raw
    # command still mentions it — conservatively rejected as an unrecognized dispatcher.
    root = conforming(tmp_path / "ccx")
    _with_extra_entry(root, "bash -c 'uvx --isolated capt-hook run PostToolUse'")
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "unrecognized capt-hook usage" in result.reason


def test_echo_capt_hook_run_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    _with_extra_entry(root, "echo capt-hook run PostToolUse")
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "unrecognized capt-hook usage" in result.reason


@pytest.mark.parametrize(
    "trailing",
    [" && /opt/audit", "&&/opt/audit", " | tee /opt/log", "; /opt/audit"],
    ids=["and-spaced", "and-glued", "pipe", "semicolon"],
)
def test_compound_run_command_is_unrecognized_not_a_bare_run(tmp_path: Path, trailing: str) -> None:
    # A capt-hook run entry carrying a shell operator is not the canonical run shape; rejecting it as
    # unrecognized (rather than a bare "run entry") is what keeps the trailing command from riding along.
    root = conforming(tmp_path / "ccx")
    _with_extra_entry(root, f"{DEFAULT_PREFIX} run Stop{trailing}")
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "unrecognized capt-hook usage" in result.reason


# --- fix #3: attach argument validation (quoted plugin-root form, layout match) --------


def attach_cmd(dir_arg: str) -> dict[str, Any]:
    return attach_only(f"{DEFAULT_PREFIX} pack attach {dir_arg}")


def nested_plugin(tmp_path: Path, dir_arg: str) -> Path:
    """A plugin whose manifest + hooks live one hooks/ level below the plugin root."""
    plugin = tmp_path / "plugin"
    hooks = plugin / "hooks"
    write_manifest(hooks, hooks=".")
    (hooks / "h.py").write_text(POST_TOOL_HOOK)
    (hooks / "hooks.json").write_text(json.dumps(attach_cmd(dir_arg)))
    write_plugin_json(plugin, CAPTAIN_DEP)
    write_marketplace(plugin, ["captain-hook"])
    return plugin


def test_attach_quoted_root_passes_root_manifest(tmp_path: Path) -> None:
    assert by_check(conforming(tmp_path / "ccx"))["hooks.json"].ok  # conforming uses the quoted root form


def test_attach_quoted_hooks_passes_nested_manifest(tmp_path: Path) -> None:
    plugin = nested_plugin(tmp_path, '"${CLAUDE_PLUGIN_ROOT}/hooks"')
    assert failed(by_check(plugin)) == []


def test_attach_unquoted_root_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, attach_cmd("${CLAUDE_PLUGIN_ROOT}"))  # missing the double quotes
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "double-quoted" in result.reason


def test_attach_wrong_dir_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, attach_cmd('"/tmp/other"'))
    assert not by_check(root)["hooks.json"].ok


def test_attach_quoted_root_with_nested_manifest_fails(tmp_path: Path) -> None:
    # The manifest is nested, so the attach must carry the /hooks suffix; the bare root form is a
    # layout mismatch even though it is correctly quoted.
    plugin = nested_plugin(tmp_path, '"${CLAUDE_PLUGIN_ROOT}"')
    result = by_check(plugin)["hooks.json"]
    assert not result.ok
    assert "malformed pack attach" in result.reason


# --- fix #4: ambiguous hooks.json resolution -------------------------------------------


def test_ambiguous_hooks_json_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")  # writes <root>/hooks/hooks.json
    (root / "hooks.json").write_text(json.dumps(attach_only()))  # decoy beside the manifest
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "ambiguous" in result.reason


# --- fix #5: dependency version-floor validation ---------------------------------------


def test_plugin_json_null_version_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook", "marketplace": "captain-hook", "version": None}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "version" in result.reason


def test_plugin_json_empty_version_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook", "marketplace": "captain-hook", "version": ""}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "version" in result.reason


def test_plugin_json_missing_name_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"marketplace": "captain-hook", "version": ">=9.8.0"}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "name" in result.reason


def test_plugin_json_pin_without_floor_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook", "marketplace": "captain-hook", "version": "9.8.0"}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "lower-bound" in result.reason


# --- fix #6: malformed inputs become check failures, never tracebacks ------------------


def test_unbalanced_quotes_hooks_json_is_a_failure(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    _with_extra_entry(root, "uvx --isolated capt-hook run 'unterminated")
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "unbalanced quotes" in result.reason


def test_malformed_plugin_json_is_a_failure(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "plugin.json").write_text("{ not valid json ")
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "unreadable" in result.reason


def test_malformed_marketplace_json_is_a_failure(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "marketplace.json").write_text("{ not valid json ")
    result = by_check(root)["marketplace.json"]
    assert not result.ok
    assert "unreadable" in result.reason


# --- fix #7: zero loaded hooks fails the load check even when async-decision also fires -


def test_async_only_hook_fails_load_and_async_checks(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hook(root, 'from captain_hook import Event, hook\n\nhook(Event.Stop, message="bg", async_=True)\n')
    results = by_check(root)
    assert not results["load"].ok
    assert "no hooks loaded" in results["load"].reason
    assert not results["async-decision"].ok  # both checks report the rejected hook
