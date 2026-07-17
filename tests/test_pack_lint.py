from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from captain_hook.cli import LintResult, lint_pack
from captain_hook.packs import manager
from tests.helpers import run_cli

pytestmark = pytest.mark.usefixtures("isolate_modules")

CAPTAIN_DEP = [{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=9.8.0"}]
POST_TOOL_HOOK = 'from captain_hook import Event, hook\n\nhook(Event.PostToolUse, message="m")\n'
# The discovery contract carries no hooks.json entry; these are the legacy lines it now rejects.
LEGACY_ATTACH = 'uvx --isolated capt-hook pack attach "${CLAUDE_PLUGIN_ROOT}"'
LEGACY_RUN = "uvx --isolated capt-hook run PostToolUse"


# --- fixture builders ----------------------------------------------------------------


def write_manifest(root: Path, *, name: str = "ccx", hooks: str = ".", marketplaces: list[str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    body = f'[pack]\nname = "{name}"\nversion = "0.1.0"\ndescription = "d"\nhooks = "{hooks}"\n'
    if marketplaces is not None:
        body += f"marketplaces = {json.dumps(marketplaces)}\n"
    (root / manager.PACK_MANIFEST).write_text(body)


def write_hook(root: Path, src: str = POST_TOOL_HOOK) -> None:
    (root / "h.py").write_text(src)


def write_hooks_json(root: Path, commands: list[str], *, event: str = "SessionStart") -> None:
    (hooks_dir := root / "hooks").mkdir(parents=True, exist_ok=True)
    data = {"hooks": {event: [{"hooks": [{"type": "command", "command": c} for c in commands]}]}}
    (hooks_dir / "hooks.json").write_text(json.dumps(data))


def write_plugin_json(root: Path, deps: list[Any]) -> None:
    (d := root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"name": "ccx", "dependencies": deps}))


def write_marketplace(root: Path, allow: list[str]) -> None:
    (d := root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / "marketplace.json").write_text(json.dumps({"name": "ccx", "allowCrossMarketplaceDependenciesOn": allow}))


def conforming(root: Path) -> Path:
    """A pack that satisfies every discovery-contract check — three artifacts, no hooks.json."""
    write_manifest(root)
    write_hook(root)
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
    # The session-start check is gone under discovery; six checks remain, no hooks.json entry required.
    assert set(results) == {"manifest", "hooks.json", "plugin.json", "marketplace.json", "load", "async-decision"}


def test_no_hooks_json_passes(tmp_path: Path) -> None:
    result = by_check(conforming(tmp_path / "ccx"))["hooks.json"]
    assert result.ok and not result.warning  # a discovered pack ships zero capt-hook invocations
    assert "no hooks.json" in result.reason


def test_conforming_pack_exits_zero(tmp_path: Path) -> None:
    result = run_cli("pack", "lint", str(conforming(tmp_path / "ccx")))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout


def test_valid_marketplaces_manifest_passes(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_manifest(root, marketplaces=["yasyf/cc-present"])
    assert by_check(root)["manifest"].ok


def test_malformed_marketplace_fails_manifest_check(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_manifest(root, marketplaces=["--evil/x"])  # a flag-injection slug fails PackManifest.load
    results = by_check(root)
    assert not results["manifest"].ok
    assert "marketplace repo" in results["manifest"].reason
    assert "manifest" in failed(results)  # the existing manifest check reports it — no dedicated lint check


# --- the discovery break: any capt-hook hooks.json entry fails -----------------------


def test_legacy_attach_line_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, [LEGACY_ATTACH])
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "discovery contract" in result.reason  # the one migration aid: delete the capt-hook entry
    assert "hooks.json" in failed(by_check(root))


def test_capt_hook_run_line_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, [LEGACY_RUN], event="PostToolUse")
    result = by_check(root)["hooks.json"]
    assert not result.ok
    assert "discovery contract" in result.reason


def test_unrelated_commands_pass(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, ["${CLAUDE_PLUGIN_ROOT}/hooks/install-binary.sh"])  # a plugin's own shell hook
    assert by_check(root)["hooks.json"].ok  # no capt-hook mention → the pack's own hooks are its business


def test_legacy_shape_exits_nonzero(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_hooks_json(root, [LEGACY_ATTACH])
    result = run_cli("pack", "lint", str(root))
    assert result.returncode == 1
    assert "FAIL" in result.stdout


# --- the deleted session-start check: a SessionStart-subscribing pack now passes ------


def test_session_start_subscribing_pack_passes(tmp_path: Path) -> None:
    # Under discovery there is no attach racing the canonical run, so a pack may subscribe SessionStart.
    root = conforming(tmp_path / "ccx")
    write_hook(root, 'from captain_hook import Event, hook\n\nhook(Event.SessionStart, message="ctx")\n')
    results = by_check(root)
    assert "session-start" not in results  # the check is gone
    assert failed(results) == []  # and its presence no longer fails the pack


# --- per-check coverage --------------------------------------------------------------


def test_missing_manifest_fails_and_skips_hook_load(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    results = by_check(root)
    assert not results["manifest"].ok
    assert not results["load"].ok  # no manifest, no hooks loaded
    assert "not loaded" in results["async-decision"].reason


def test_pack_with_no_hooks_fails_load(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / "h.py").unlink()
    result = by_check(root)["load"]
    assert not result.ok
    assert "no hooks loaded" in result.reason
    assert run_cli("pack", "lint", str(root)).returncode == 1


def test_syntax_broken_hook_file_fails_load(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / "h.py").write_text("def broken(:\n")  # syntax error
    result = by_check(root)["load"]
    assert not result.ok
    assert "failed to load" in result.reason
    assert run_cli("pack", "lint", str(root)).returncode == 1


def test_nonexistent_hooks_dir_fails_load(tmp_path: Path) -> None:
    root = tmp_path / "ccx"
    write_manifest(root, hooks="nope")
    write_plugin_json(root, CAPTAIN_DEP)
    write_marketplace(root, ["captain-hook"])
    assert not by_check(root)["load"].ok
    assert run_cli("pack", "lint", str(root)).returncode == 1


def test_plugin_json_without_dependency_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "something-else"}])
    assert not by_check(root)["plugin.json"].ok


def test_plugin_json_name_only_dependency_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook"}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "marketplace" in result.reason and "version" in result.reason


def test_plugin_json_string_form_dependency_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, ["captain-hook"])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "version" in result.reason


def test_plugin_json_pin_without_floor_fails(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    write_plugin_json(root, [{"name": "captain-hook", "marketplace": "captain-hook", "version": "9.8.0"}])
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "lower-bound" in result.reason


def test_malformed_plugin_json_is_a_failure(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "plugin.json").write_text("{ not valid json ")
    result = by_check(root)["plugin.json"]
    assert not result.ok
    assert "unreadable" in result.reason


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


def test_malformed_marketplace_json_is_a_failure(tmp_path: Path) -> None:
    root = conforming(tmp_path / "ccx")
    (root / ".claude-plugin" / "marketplace.json").write_text("{ not valid json ")
    result = by_check(root)["marketplace.json"]
    assert not result.ok
    assert "unreadable" in result.reason


def test_async_hook_on_decision_event_fails(tmp_path: Path) -> None:
    # The registration guard raises on import, so the module lands in load_errors — lint surfaces it.
    root = conforming(tmp_path / "ccx")
    write_hook(root, 'from captain_hook import Event, hook\n\nhook(Event.Stop, message="bg", async_=True)\n')
    results = by_check(root)
    assert not results["async-decision"].ok
    assert "discarded" in results["async-decision"].reason
    assert not results["load"].ok  # zero hooks loaded fails load too
    assert "no hooks loaded" in results["load"].reason


def test_nested_manifest_layout_resolves(tmp_path: Path) -> None:
    # When the manifest lives in the plugin's hooks/ dir, plugin.json/marketplace.json resolve upward;
    # a hooks.json beside it is fine as long as it carries no capt-hook entry.
    plugin = tmp_path / "plugin"
    hooks = plugin / "hooks"
    write_manifest(hooks, hooks=".")
    (hooks / "h.py").write_text(POST_TOOL_HOOK)
    write_plugin_json(plugin, CAPTAIN_DEP)
    write_marketplace(plugin, ["captain-hook"])
    assert failed(by_check(hooks)) == []  # lint receives the hooks/ dir, matching discovery's probe
