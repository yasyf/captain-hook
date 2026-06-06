from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import ClassVar


class SessionCache:
    """Resolves UUID-keyed inline-test fixtures to a cached transcript jsonl.

    On miss, searches ``~/.claude/projects/*/<uuid>.jsonl`` and copies the
    discovered file into ``<root>/.claude/hook-fixtures/<uuid>.jsonl`` so future
    runs are self-contained.
    """

    UUID_RE: ClassVar[re.Pattern[str]] = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    DEFAULT_CLAUDE_PROJECTS: ClassVar[Path] = Path.home() / ".claude" / "projects"

    def __init__(self, root: Path, claude_projects: Path = DEFAULT_CLAUDE_PROJECTS) -> None:
        self.dir = root / ".claude" / "hook-fixtures"
        self.claude_projects = claude_projects

    @classmethod
    def for_root(cls, root: Path | str | None = None) -> SessionCache:
        return cls(Path(root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()))

    def path(self, uuid: str) -> Path:
        return self.dir / f"{uuid}.jsonl"

    def has(self, uuid: str) -> bool:
        return self.path(uuid).exists()

    def load(self, uuid: str) -> Path | None:
        if not self.UUID_RE.match(uuid):
            return None
        cached = self.path(uuid)
        if cached.exists():
            return cached
        if not (source := self.discover(uuid)):
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, cached)
        return cached

    def discover(self, uuid: str) -> Path | None:
        return next(self.claude_projects.glob(f"*/{uuid}.jsonl"), None)
