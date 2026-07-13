from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.testing.session_cache import SessionCache

UUID = "12345678-1234-1234-1234-123456789abc"


class TestPath:
    def test_path_under_hook_fixtures_dir(self, tmp_path: Path) -> None:
        cache = SessionCache(tmp_path, claude_projects=tmp_path / "claude")
        assert cache.path(UUID) == tmp_path / ".claude" / "hook-fixtures" / f"{UUID}.jsonl"


class TestLoad:
    def test_returns_cached_path_on_hit(self, tmp_path: Path) -> None:
        cache = SessionCache(tmp_path, claude_projects=tmp_path / "claude")
        cache.dir.mkdir(parents=True, exist_ok=True)
        cache.path(UUID).write_text('{"already":"cached"}')
        assert cache.load(UUID) == cache.path(UUID)

    def test_returns_none_when_no_cache_and_no_local(self, tmp_path: Path) -> None:
        cache = SessionCache(tmp_path, claude_projects=tmp_path / "claude")
        assert cache.load(UUID) is None

    def test_invalid_uuid_returns_none(self, tmp_path: Path) -> None:
        cache = SessionCache(tmp_path, claude_projects=tmp_path / "claude")
        assert cache.load("not-a-uuid") is None
        assert cache.load("0316cd66-bd39-4432-b40b") is None  # truncated

    def test_fetches_from_local_claude_projects_and_caches(self, tmp_path: Path) -> None:
        claude = tmp_path / "claude"
        slug = claude / "-Users-yasyf-some-workspace"
        slug.mkdir(parents=True)
        source = slug / f"{UUID}.jsonl"
        source.write_text('{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}\n')

        cache = SessionCache(tmp_path, claude_projects=claude)
        result = cache.load(UUID)
        assert result == cache.path(UUID)
        assert result.exists()  # type: ignore[union-attr]
        assert result.read_text() == source.read_text()  # type: ignore[union-attr]

    def test_subsequent_load_uses_cache_not_source(self, tmp_path: Path) -> None:
        claude = tmp_path / "claude"
        slug = claude / "-Users-yasyf-w"
        slug.mkdir(parents=True)
        source = slug / f"{UUID}.jsonl"
        source.write_text('{"line":"original"}\n')

        cache = SessionCache(tmp_path, claude_projects=claude)
        cache.load(UUID)
        source.write_text('{"line":"modified"}\n')
        assert cache.load(UUID) == cache.path(UUID)
        assert cache.path(UUID).read_text() == '{"line":"original"}\n'


class TestForRoot:
    def test_uses_claude_project_dir_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cache = SessionCache.for_root()
        assert cache.dir == tmp_path / ".claude" / "hook-fixtures"

    def test_explicit_root_overrides_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/elsewhere")
        cache = SessionCache.for_root(tmp_path)
        assert cache.dir == tmp_path / ".claude" / "hook-fixtures"
