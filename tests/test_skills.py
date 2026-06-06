from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
import yaml

import captain_hook
from captain_hook.cli import install_skills, maybe_launch_bootstrap, register_marketplace

SKILLS_DIR = Path(captain_hook.__file__).parent / "skills"
SKILL_DIRS = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())
EXPECTED_SKILLS = {"bootstrapping-hooks", "translating-styleguides"}
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
EXTERNAL_LINK = re.compile(r"^(https?:|mailto:|#)")


def parse_skill(skill_dir: Path) -> tuple[dict[str, Any], str]:
    text = (skill_dir / "SKILL.md").read_text()
    assert text.startswith("---\n"), f"{skill_dir.name}/SKILL.md missing frontmatter"
    frontmatter, body = text.removeprefix("---\n").split("\n---\n", 1)
    return yaml.safe_load(frontmatter), body


def tree_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda p: p.name)
class TestSkillValidation:
    def test_name_matches_directory(self, skill_dir: Path) -> None:
        meta, _ = parse_skill(skill_dir)
        assert meta["name"] == skill_dir.name
        assert SKILL_NAME.fullmatch(meta["name"])
        assert len(meta["name"]) <= 64

    def test_description_within_limit(self, skill_dir: Path) -> None:
        meta, _ = parse_skill(skill_dir)
        assert isinstance(meta["description"], str)
        assert 0 < len(meta["description"]) <= 1024

    def test_body_under_500_lines(self, skill_dir: Path) -> None:
        _, body = parse_skill(skill_dir)
        assert len(body.splitlines()) < 500

    def test_relative_links_resolve(self, skill_dir: Path) -> None:
        for md_file in skill_dir.rglob("*.md"):
            for target in MARKDOWN_LINK.findall(md_file.read_text()):
                if EXTERNAL_LINK.match(target):
                    continue
                resolved = (md_file.parent / target.split("#")[0]).resolve()
                assert resolved.is_file(), f"{md_file.relative_to(skill_dir)} links to missing {target}"
                assert resolved.is_relative_to(skill_dir.resolve()), (
                    f"{md_file.relative_to(skill_dir)} links outside the skill dir: {target}"
                )


def test_expected_skills_present() -> None:
    assert {d.name for d in SKILL_DIRS} == EXPECTED_SKILLS


class TestInstallSkills:
    def test_installs_all_skills(self, tmp_path: Path) -> None:
        summary = install_skills(tmp_path)
        assert summary == {name: "installed" for name in EXPECTED_SKILLS}
        for name in EXPECTED_SKILLS:
            dest = tmp_path / ".claude" / "skills" / name
            assert (dest / "SKILL.md").is_file()
            assert tree_files(dest) == tree_files(SKILLS_DIR / name)

    def test_skip_preserves_local_edits(self, tmp_path: Path) -> None:
        install_skills(tmp_path)
        edited = tmp_path / ".claude" / "skills" / "bootstrapping-hooks" / "SKILL.md"
        edited.write_text("# sentinel\n")
        summary = install_skills(tmp_path)
        assert summary == {name: "skipped" for name in EXPECTED_SKILLS}
        assert edited.read_text() == "# sentinel\n"

    def test_force_replaces_and_removes_strays(self, tmp_path: Path) -> None:
        install_skills(tmp_path)
        skill_dir = tmp_path / ".claude" / "skills" / "translating-styleguides"
        stray = skill_dir / "stray.md"
        stray.write_text("stray\n")
        summary = install_skills(tmp_path, force=True)
        assert summary == {name: "replaced" for name in EXPECTED_SKILLS}
        assert not stray.exists()
        assert tree_files(skill_dir) == tree_files(SKILLS_DIR / "translating-styleguides")


class TestMaybeLaunchBootstrap:
    def test_skips_without_tty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
        assert maybe_launch_bootstrap(tmp_path) is False

    def test_skips_without_claude_on_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(shutil, "which", lambda _: None)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
        assert maybe_launch_bootstrap(tmp_path) is False

    def test_launches_on_confirm(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
        calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
        assert maybe_launch_bootstrap(tmp_path) is True
        assert calls == [(["claude", "/bootstrapping-hooks"], {"cwd": tmp_path, "check": False})]
        settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}

    def test_respects_decline(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
        assert maybe_launch_bootstrap(tmp_path) is False


class TestRegisterMarketplace:
    def test_creates_settings(self, tmp_path: Path) -> None:
        register_marketplace(tmp_path)
        settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert settings["extraKnownMarketplaces"]["captain-hook"]["source"] == {
            "source": "github",
            "repo": "yasyf/captain-hook",
        }
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}

    def test_merges_existing_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({
            "hooks": {"PreToolUse": []},
            "extraKnownMarketplaces": {"skills": {"source": {"source": "github", "repo": "yasyf/skills"}}},
            "enabledPlugins": {"codex@skills": True},
        }))
        register_marketplace(tmp_path)
        settings = json.loads(settings_path.read_text())
        assert settings["hooks"] == {"PreToolUse": []}
        assert set(settings["extraKnownMarketplaces"]) == {"skills", "captain-hook"}
        assert settings["enabledPlugins"] == {"codex@skills": True, "captain-hook@captain-hook": True}


class TestPluginManifests:
    def test_plugin_manifest(self) -> None:
        manifest = json.loads((SKILLS_DIR.parent / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == "captain-hook"
        assert SKILL_NAME.fullmatch(manifest["name"])
        assert "version" not in manifest
        assert not {"skills", "mcpServers", "hooks"} & manifest.keys()

    def test_marketplace_manifest(self) -> None:
        manifest = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        assert manifest["name"] == "captain-hook"
        assert manifest["owner"]["name"]
        (plugin,) = manifest["plugins"]
        assert plugin["name"] == "captain-hook"
        assert plugin["source"] == "./captain_hook"
        plugin_root = REPO_ROOT / "captain_hook"
        assert (plugin_root / ".claude-plugin" / "plugin.json").is_file()
        assert (plugin_root / "skills").resolve() == SKILLS_DIR.resolve()
