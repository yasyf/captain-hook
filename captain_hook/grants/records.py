"""The grant record: what the owner permitted, where their words live, and every use of it."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ANY = "*"
ScopeValue = str | list[str]
BRIEF_CHARS = 160
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
QUOTE_MARK = re.compile(r"[\"“”]|(?<!\w)['‘’]|['‘’](?!\w)")


RECORD_ID = re.compile(
    r"\[?\b(?:ask|words|ccn|board|teammate|shell|created|toolu)[:_][\w#@.:/-]+\]?"
    r"|(?<![\w-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])"
)
CLOCK = re.compile(r"\b20\d\d-\d\d-\d\d\b|\b\d{1,2}:\d\d(?::\d\d)?\s?(?:Z|UTC|PT|PDT|PST|am|pm)?\b", re.IGNORECASE)
RULING_WORD = re.compile(r"\b([Rr])uling(s?)\b")
SPACE_BEFORE_MARK = re.compile(r"\s+([.,;:!?])")


def brief(text: str) -> str:
    """*text*'s first sentence for a block message: no quotation marks, record ids, or times, clipped at a word."""
    plain = CLOCK.sub("a stated time", RECORD_ID.sub("", QUOTE_MARK.sub("", text.strip())))
    plain = RULING_WORD.sub(lambda word: ("D" if word[1] == "R" else "d") + "ecision" + word[2], plain)
    sentence = SENTENCE_END.split(SPACE_BEFORE_MARK.sub(r"\1", " ".join(plain.split())), maxsplit=1)[0]
    if len(sentence) > BRIEF_CHARS:
        sentence = sentence[:BRIEF_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return sentence if sentence.endswith((".", "!", "?", "…")) else f"{sentence}."


def matches(allowed: ScopeValue, value: str) -> bool:
    """Whether a grant's scope value admits *value*: itself, any non-empty value for ``*``, or a member of a set."""
    if isinstance(allowed, list):
        return value in allowed
    return allowed == value or (allowed == ANY and value != "")


def covers(scope: Mapping[str, ScopeValue], action: Mapping[str, str]) -> bool:
    """Whether a grant's *scope* admits every scope value of an action, key for key."""
    return set(scope) == set(action) and all(matches(scope[key], str(action[key])) for key in scope)


def render_scope(scope: Mapping[str, ScopeValue]) -> str:
    """A scope as ``key=value`` pairs, a set as ``{a, b}`` and ``*`` as any."""
    shown = (
        f"{key}={{{', '.join(value)}}}"
        if isinstance(value, list)
        else f"{key}={'any' if value == ANY else value or 'none'}"
        for key, value in scope.items()
    )
    return ", ".join(shown) or "any action"


class Evidence(BaseModel):
    """One piece of the owner's own words a grant rests on.

    Attributes:
        id: Stable handle the judge cites in ``relied_on``, e.g. ``ask:toolu_01#2`` or ``ccn:543e865``.
        source: Where the words live: ``ask``, ``words``, ``ccn-answer``, ``cli``, or a hook's own source.
        quote: The owner's words, verbatim.
        said_at: When the owner said them, when the source records it.
        detail: Context the judge reads beside the quote, such as the question and the options shown.
        approves: The words the owner approved word for word: their own words, or the label, description,
            and preview of the option an answer picked by its label with no notes.
        asked: The question an answer replies to, which can name where the approved words go.
        key: The approval's identity; every grant minted from it shares one budget.
        live: Whether its source collects it again before a stored grant spends; a grant whose live
            evidence the source no longer collects covers nothing.
    """

    id: str
    source: str
    quote: str
    said_at: datetime | None = None
    detail: str = ""
    approves: str = ""
    asked: str = ""
    key: str = ""
    live: bool = False


class Grant(BaseModel):
    """A spendable permission the owner gave, bound to the session tree it was given in.

    Attributes:
        id: Twelve hex characters, named in every allow and deny.
        kind: The declaration that may spend it, e.g. ``slack.write``.
        tree: The root session id of the tree it was given in; only that tree spends it.
        scope: The declaration's scope keys, each an exact value, ``*`` for any non-empty value, or a set
            of values; an action is covered when every one of its scope values is admitted.
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
    scope: dict[str, ScopeValue]
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


class Adoption(BaseModel):
    """An agent in another session tree made a grant usable there; the two trees share its budget."""

    grant_id: str
    tree: str
    at: datetime
    session: str
    agent: str


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
    """A grant covers the action; its use is reserved and commits when the event is allowed.

    Attributes:
        grant: The grant that covers the action.
        remaining: Uses left after this one, ``None`` when unlimited.
        reason: Why the grant covers the action.
        unjudged: What stopped the judge, when the action goes ahead under a declaration that fails open.
    """

    grant: Grant
    remaining: int | None
    reason: str
    unjudged: str = ""

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Denied:
    """No grant covers the action: why, what would allow it, and whether the judge failed to decide.

    Attributes:
        reason: Why nothing covered the action, one sentence for the agent that proposed it.
        would_allow: What the agent can do to get permission.
        undecided: The judge gave no verdict, so the same action may be retried.
        detail: Every refusal and the judge's full reasoning, for the user and the logs.
    """

    reason: str
    would_allow: str
    undecided: bool = False
    detail: str = ""

    def __bool__(self) -> bool:
        return False

    @property
    def message(self) -> str:
        """The reason and the remediation: two sentences that meet a block message's copy bar."""
        return f"{brief(self.reason)} {self.would_allow}".strip() if self.reason else self.would_allow

    @property
    def explained(self) -> str:
        """Every refusal behind this denial in full, for the user."""
        return self.detail or f"{self.reason} {self.would_allow}".strip()
