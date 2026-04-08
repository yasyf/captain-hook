from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True, kw_only=True)
class File:
    """A file path wrapper with glob matching, prefix checks, and test-file detection.

    Delegates ``Path`` methods via ``__getattr__`` so ``.suffix``, ``.name``,
    ``.parent``, ``.exists()`` etc. work directly.
    """

    path: Path

    TEST_PATTERNS: ClassVar[list[str]] = ["**/test_*.py", "**/conftest.py", "**/tests/**/*.py"]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.path, name)

    def __str__(self) -> str:
        return str(self.path)

    def __fspath__(self) -> str:
        return str(self.path)

    def __eq__(self, other: object) -> bool:
        match other:
            case File(path=p):
                return self.path == p
            case Path():
                return self.path == other
            case _:
                return NotImplemented

    def __hash__(self) -> int:
        return hash(self.path)

    @cached_property
    def is_test(self) -> bool:
        return self.matches(*self.TEST_PATTERNS)

    def matches(self, *patterns: str) -> bool:
        s, name = str(self.path), self.path.name
        return any(fnmatch(s, p) or fnmatch(name, p) for p in patterns)

    def under(self, *prefixes: str) -> bool:
        s = str(self.path)
        return any(s.startswith(p) or f"/{p}" in s for p in prefixes)

    def exists(self) -> bool:
        return self.path.exists()

    def read_text(self) -> str:
        return self.path.read_text()

    def contains(self, pattern: str) -> bool:
        try:
            return bool(re.search(pattern, self.read_text()))
        except (OSError, UnicodeDecodeError):
            return False


@dataclass(frozen=True, kw_only=True)
class PathMatcher:
    """A reusable set of glob patterns for matching file paths. Supports ``in`` operator."""

    patterns: list[str]

    def matches(self, path: str | Path | File) -> bool:
        match path:
            case File():
                return path.matches(*self.patterns)
            case _:
                return File(path=Path(path)).matches(*self.patterns)

    def __contains__(self, path: str | Path | File) -> bool:
        return self.matches(path)
