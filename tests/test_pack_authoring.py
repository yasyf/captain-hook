from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from captain_hook.cli import cli

VALID_DEP = {"dependencies": [{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=11.0.0"}]}
GOOD_HOOK = (
    "from captain_hook.app import hook\n"
    "from captain_hook.types import Event\n"
    "from captain_hook.testing.types import Block, Input\n\n"
    'hook(Event.PreToolUse, message="x", block=True, tests={Input(command="echo hi"): Block()})\n'
)
# Blocks the command but its inline test expects Allow() — the inline test fails.
FAILING_HOOK = (
    "from captain_hook.app import hook\n"
    "from captain_hook.types import Event\n"
    "from captain_hook.testing.types import Allow, Input\n\n"
    'hook(Event.PreToolUse, message="x", block=True, tests={Input(command="echo hi"): Allow()})\n'
)


def make_plugin_root(
    tmp_path: Path,
    *,
    plugin_json: dict[str, object] | None = None,
    descriptor: str | None = "resources = []\n",
    hook_src: str = GOOD_HOOK,
    hooks_dir: bool = True,
    hook_file: bool = True,
) -> Path:
    root = tmp_path / "plug"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(plugin_json if plugin_json is not None else VALID_DEP)
    )
    pack = root / "capt-hook"
    if descriptor is not None:
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "pack.toml").write_text(descriptor)
    if hooks_dir:
        (hooks := pack / "hooks").mkdir(parents=True, exist_ok=True)
        if hook_file:
            (hooks / "guard.py").write_text(hook_src)
    return root


def run_pack_test(root: Path) -> object:
    return CliRunner().invoke(cli, ["pack", "test", str(root)])


def test_pack_test_accepts_a_valid_pack(tmp_path: Path, isolate_modules: None) -> None:
    result = run_pack_test(make_plugin_root(tmp_path))
    assert result.exit_code == 0, result.output
    assert "1 tests: 1 passed" in result.output


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        pytest.param({"descriptor": None}, "not a pack plugin root", id="missing-pack-toml"),
        pytest.param({"hooks_dir": False}, "not a pack plugin root", id="missing-hooks-dir"),
        pytest.param({"hook_file": False}, "no hooks loaded", id="empty-hooks-dir"),
        pytest.param({"descriptor": "[tools.x]\n"}, "behaves_like", id="malformed-descriptor"),
        pytest.param(
            {
                "plugin_json": {
                    "dependencies": [{"name": "captain-hook", "marketplace": "captain-hook", "version": "11.0.0"}]
                }
            },
            "lower-bound",
            id="dependency-floor-violation",
        ),
        pytest.param({"hook_src": FAILING_HOOK}, "1 failed", id="failing-inline-test"),
    ],
)
def test_pack_test_rejects(tmp_path: Path, isolate_modules: None, kwargs: dict[str, object], needle: str) -> None:
    result = run_pack_test(make_plugin_root(tmp_path, **kwargs))
    assert result.exit_code != 0, result.output
    assert needle in result.output
