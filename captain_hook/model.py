from __future__ import annotations

import re
from dataclasses import dataclass

CLAUDE_MODEL = re.compile(r"claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-\d{8})?(?:\[\w+\])?")


@dataclass(frozen=True)
class Model:
    """The model a session runs on, parsed from the id its transcript records.

    ``family`` and ``version`` are set for a ``claude-<family>-<major>[-<minor>]`` id, such as
    ``claude-opus-5-5`` or ``claude-sonnet-4-5-20250929``, and ``None`` for any other id.

    Example:
        >>> ctx.model is not None and ctx.model.at_least("opus", 5, 5)
    """

    id: str
    family: str | None
    version: tuple[int, int] | None

    @classmethod
    def parse(cls, id: str) -> Model:
        if (match := CLAUDE_MODEL.fullmatch(id)) is None:
            return cls(id, None, None)
        return cls(id, match["family"], (int(match["major"]), int(match["minor"] or 0)))

    def at_least(self, family: str, major: int, minor: int = 0) -> bool:
        """Whether this is ``family`` at version ``major.minor`` or later."""
        return self.family == family and self.version is not None and self.version >= (major, minor)
