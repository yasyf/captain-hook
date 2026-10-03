"""The grant record: what the owner permitted, where their words live, and every use of it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """One piece of the owner's own words a grant rests on.

    Attributes:
        id: Stable handle the judge cites in ``relied_on``, e.g. ``ask:toolu_01#2`` or ``ccn:543e865``.
        source: Where the words live: ``ask``, ``words``, ``ccn-answer``, ``cli``, or a hook's own source.
        quote: The owner's words, verbatim.
        said_at: When the owner said them, when the source records it.
        detail: Context the judge reads beside the quote, such as the question and the options shown.
        key: The approval's identity; every grant minted from it shares one budget.
    """

    id: str
    source: str
    quote: str
    said_at: datetime | None = None
    detail: str = ""
    key: str = ""


class Grant(BaseModel):
    """A spendable permission the owner gave, bound to the session tree it was given in.

    Attributes:
        id: Twelve hex characters, named in every allow and deny.
        kind: The declaration that may spend it, e.g. ``slack.write``.
        tree: The root session id of the tree it was given in; only that tree spends it.
        scope: Exact values of the declaration's scope keys that an action must carry.
        approved: The exact payload the owner saw, for content rules and the judge's diff.
        uses: Uses the grant allows in all; ``None`` is unlimited.
        expires: When it stops covering anything.
        rules: Names of declared rules this grant asserts on top of the always-on ones.
        evidence: The owner's words it rests on.
        source_key: The approval it was minted from; one approval mints one grant.
        links: Ids of the same permission in other systems, such as a daemon's own grant.
        author: Who minted it.
        created: When it was minted.
        revoked: When it was revoked.
    """

    id: str
    kind: str
    tree: str
    scope: dict[str, str]
    approved: dict[str, Any] | None = None
    uses: int | None = 1
    expires: datetime | None = None
    rules: list[str] = Field(default_factory=list[str])
    evidence: list[Evidence] = Field(default_factory=list[Evidence])
    source_key: str | None = None
    links: dict[str, str] = Field(default_factory=dict[str, str])
    author: str
    created: datetime
    revoked: datetime | None = None

    @property
    def standing(self) -> bool:
        return self.uses is None


SpendState = Literal["reserved", "committed", "released"]


class Spend(BaseModel):
    """One use of a grant: reserved while its event is decided, then committed or released."""

    id: int
    grant_id: str
    at: datetime
    state: SpendState
    session: str
    agent: str
    tool_use_id: str
    fingerprint: str
    summary: str
    reason: str
    relied_on: list[str] = Field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class Proposal:
    """The action a hook asks a grant to cover.

    Attributes:
        scope: The values of the declaration's scope keys, e.g. ``{"channel": "C0…", "thread": ""}``.
        payload: Every effect-bearing field, compared whole against a grant's ``approved``.
        summary: One line naming the action in spend logs and messages.
    """

    scope: Mapping[str, str]
    payload: Mapping[str, Any] = field(default_factory=dict[str, Any])
    summary: str = ""


@dataclass(frozen=True, slots=True)
class Allowed:
    """A grant covers the action; its use is reserved and commits when the event is allowed."""

    grant: Grant
    remaining: int | None
    reason: str

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Denied:
    """No grant covers the action: why, and what would allow it."""

    reason: str
    would_allow: str

    def __bool__(self) -> bool:
        return False

    @property
    def message(self) -> str:
        return f"{self.reason} {self.would_allow}".strip()
