from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.layout import LayoutSettings
from captain_hook.loader import discover_pack
from captain_hook.packs import manager
from tests.test_pack_performance_search import decision

LAYOUT = {
    "HOOKS_LAYOUT_PROTECTED_ROOTS": '["~/.local/share/jobs", "~/src"]',
    "HOOKS_LAYOUT_SEARCHABLE_UNITS": '["~/.local/share/jobs/*/payload"]',
    "HOOKS_LAYOUT_CHECKOUT_COLLECTIONS": '["~/src"]',
    "HOOKS_LAYOUT_WORKTREE_POOLS": '{"~/lanes": 1}',
}
GENERIC = [
    pytest.param("rg -l needle ~/.cache/tool -g job.json", "deny", id="cache-root"),
    pytest.param("find ~ -name job.json", "deny", id="home"),
    pytest.param("rg needle ~/.claude/worktrees/captain-hook", "deny", id="builtin-pool-aggregate"),
]


@pytest.fixture
def performance(isolate_modules: None) -> None:
    discover_pack("performance", manager.resolve_builtin("performance").path)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.resolve() / "home"
    (root / ".local" / "share" / "jobs" / "7f3c" / "payload").mkdir(parents=True)
    (root / ".local" / "share" / "jobs" / "7f3c" / "job.json").write_text("{}\n")
    (root / "src" / "app").mkdir(parents=True)
    (root / "lanes" / "app" / "lane").mkdir(parents=True)
    (hooks := root / "lanes" / "monorepo" / "lane" / "tools" / "hooks").mkdir(parents=True)
    (hooks / "warm-lane.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(root))
    for name in LayoutSettings.model_fields:
        monkeypatch.delenv(f"HOOKS_LAYOUT_{name.upper()}", raising=False)
    return root


@pytest.fixture
def configured(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, value in LAYOUT.items():
        monkeypatch.setenv(name, value)
    return home


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        *GENERIC,
        pytest.param("rg -l watcher-poll ~/.local/share/jobs/7f3c -g job.json", None, id="unlisted-root"),
        pytest.param("rg -n needle ~/src -g '*.go'", None, id="unlisted-collection-search"),
        pytest.param("find ~/src -name job.json", None, id="unlisted-collection-find"),
        pytest.param("rg needle ~/lanes/monorepo", None, id="unlisted-pool-aggregate"),
        pytest.param("find ~/lanes -name job.json", None, id="unlisted-pool-find"),
        pytest.param("cd ~/lanes/monorepo/lane && yarn install", None, id="unlisted-pool-install"),
    ],
)
def test_empty_layout_guards_only_generic_roots(
    performance: None, home: Path, tmp_path: Path, command: str, expected: str | None
) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        *GENERIC,
        pytest.param("rg -l watcher-poll ~/.local/share/jobs -g job.json", "deny", id="protected-root"),
        pytest.param("rg -l watcher-poll ~/.local/share/jobs/7f3c -g job.json", "deny", id="below-protected-root"),
        pytest.param("find ~/.local/share/jobs -name job.json", "deny", id="find-protected-root"),
        pytest.param("rg -n needle ~/.local/share/jobs/7f3c/payload", None, id="searchable-unit"),
        pytest.param("rg -n needle ~/src -g '*.go'", "deny", id="collection-search"),
        pytest.param("rg -n needle ~/src/app -g '*.go'", None, id="collection-checkout-search"),
        pytest.param("find ~/src -name job.json", "deny", id="collection-find"),
        pytest.param("find ~/src/app -name job.json", None, id="collection-checkout-find"),
        pytest.param("rg needle ~/lanes/monorepo", "deny", id="pool-aggregate"),
        pytest.param("rg needle ~/lanes/monorepo/lane", None, id="pool-lane"),
        pytest.param("find ~/lanes -name job.json", "deny", id="pool-find"),
        pytest.param("find ~/lanes/monorepo/lane -name job.json", None, id="pool-lane-find"),
        pytest.param("cd ~/lanes/monorepo/lane && yarn install", "deny", id="warm-lane-install"),
        pytest.param("npm --prefix ~/lanes/monorepo/lane ci", "deny", id="warm-lane-prefix-install"),
        pytest.param("cd ~/lanes/app/lane && yarn install", None, id="lane-without-warm-lane"),
        pytest.param("yarn --cwd ~/src/app install", None, id="checkout-outside-pools"),
        pytest.param("cd ~/lanes/monorepo/lane && yarn install --mode=update-lockfile", None, id="lockfile-only"),
    ],
)
def test_configured_layout_guards_listed_roots(
    performance: None, configured: Path, tmp_path: Path, command: str, expected: str | None
) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) == expected
