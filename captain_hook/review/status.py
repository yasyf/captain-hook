from __future__ import annotations

from enum import StrEnum


class CandidateStatus(StrEnum):
    """A candidate's lifecycle state; ``REJECTED`` is terminal and ``ACCEPTED`` reopens only on recurrence."""

    WATCHING = "watching"
    PR_OPEN = "pr_open"
    STALE = "stale"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
