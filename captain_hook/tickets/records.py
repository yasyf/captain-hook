"""The ticket record: what a rule found, where it reaches, and the uses recorded against it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field


class Reach(StrEnum):
    """How far a ticket reaches: one action, or every later action at its place."""

    ONCE = "once"
    PLACE = "place"


class Evidence(BaseModel):
    """One thing a rule saw: the user's words, their answer, a ruling, or a record a lookup returned.

    Attributes:
        id: Stable handle a judge cites, e.g. ``words:1a2b3c4d5e6f`` or ``ask:toolu_01#0``.
        source: Where it came from: ``prompt``, ``queued``, ``plan``, ``ask``, ``board``, ``ccn``, or a hook's own.
        quote: The words themselves, verbatim.
        said_at: When they were said, when the source records it.
        detail: Context read beside the quote: the agent's message it answers, the question and options shown.
    """

    id: str
    source: str
    quote: str
    said_at: datetime | None = None
    detail: str = ""


class Answer(Evidence):
    """An answer to a question the agent asked: ``AskUserQuestion``, a cc-present board card, or a cc-slack card.

    Attributes:
        question: The question the user answered.
        options: Every option shown, label to description.
        chosen: The labels the user picked.
        typed: What the user typed beside or instead of a pick.
    """

    question: str
    options: dict[str, str] = Field(default_factory=dict[str, str])
    chosen: tuple[str, ...] = ()
    typed: str = ""


@dataclass(frozen=True, slots=True)
class Proposal:
    """What a hook asks a ticket to cover.

    Attributes:
        place: The values of the declaration's place keys, e.g. ``{"channel": "C0…", "thread": ""}``.
        payload: Every effect-bearing field; a once ticket is bound to its fingerprint.
        summary: One line naming the action in logs and messages.
    """

    place: Mapping[str, str]
    payload: Mapping[str, Any] = field(default_factory=dict[str, Any])
    summary: str = ""

    @property
    def fingerprint(self) -> str:
        material = json.dumps([dict(self.place), dict(self.payload)], sort_keys=True, default=str)
        return sha256(material.encode()).hexdigest()[:16]


class Ticket(BaseModel):
    """A record that one action, or every later action at one place, goes ahead.

    Attributes:
        id: Twelve hex characters, named in every allow.
        kind: The declaration that minted it, e.g. ``slack.write``.
        tree: The root session id of the tree it was minted in; that tree and the trees reading it spend it.
        place: The declaration's place keys, each an exact value.
        reach: ``once`` covers the action whose fingerprint it records; ``place`` covers every action at its place.
        rule: The rule that minted it.
        evidence: What the rule found.
        fingerprint: The action a once ticket covers.
        author: Who minted it.
        created: When it was minted.
        expires: When it stops covering anything.
        revoked: When it was revoked.
    """

    id: str
    kind: str
    tree: str
    place: dict[str, str]
    reach: Reach
    rule: str
    evidence: list[Evidence] = Field(default_factory=list[Evidence])
    fingerprint: str | None = None
    author: str
    created: datetime
    expires: datetime | None = None
    revoked: datetime | None = None

    def covers(self, place: Mapping[str, str], fingerprint: str) -> bool:
        return dict(self.place) == dict(place) and (self.reach is Reach.PLACE or self.fingerprint == fingerprint)


UseState = Literal["committed", "released"]


class Use(BaseModel):
    """One action a ticket went ahead for, recorded by the hook or the system that spends it downstream."""

    id: int
    ticket_id: str
    at: datetime
    state: UseState
    session: str
    agent: str
    call: str
    fingerprint: str
    summary: str


@dataclass(frozen=True, slots=True)
class Allow:
    """A ticket covers the action.

    Attributes:
        ticket: The ticket found or minted.
        minted: Whether this decision minted it.
    """

    ticket: Ticket
    minted: bool

    def __bool__(self) -> bool:
        return True

    @property
    def reason(self) -> str:
        verb = "minted by" if self.minted else "found for"
        return f"ticket {self.ticket.id} {verb} rule {self.ticket.rule}"


@dataclass(frozen=True, slots=True)
class Deny:
    """No ticket covers the action: what the agent should do, and what each rule saw, for the user.

    Attributes:
        message: What would allow the action, for the agent.
        detail: Why each rule passed, for the user and the logs.
    """

    message: str
    detail: str = ""

    def __bool__(self) -> bool:
        return False


def render_place(place: Mapping[str, str]) -> str:
    return ", ".join(f"{key}={value or 'none'}" for key, value in place.items()) or "any action"
