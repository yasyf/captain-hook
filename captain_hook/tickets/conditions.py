"""Conditions a ticket rule is made of: each names the evidence it finds in a :class:`Context`.

They compose with :class:`~captain_hook.And`, :class:`~captain_hook.Or`, and :class:`~captain_hook.Not` like
any other condition, and a plain condition such as :class:`~captain_hook.Tool` holds in a rule too; only
ticket conditions contribute evidence. :class:`~captain_hook.UserSaid` in a rule reads the user's words
across the session tree, root session first, and contributes the words that match.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from captain_hook.conditions import UserSaid, check_condition
from captain_hook.tickets.context import Context, holds, names
from captain_hook.tickets.records import Evidence
from captain_hook.types import And, CustomCondition, Not, Or

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import Clause
    from captain_hook.tickets.records import Answer
    from captain_hook.types import TCondition

HOLD = re.compile(r"\b(?:no|don['’]?t|do not|not yet|never|hold|wait|skip|cancel)\b", re.IGNORECASE)
OPTION_NUMBER = re.compile(r"^\s*\d+\b[\s,;:.-]*")


@runtime_checkable
class Matcher(Protocol):
    """A pattern that reads the action: whether *text* says what the condition looks for at this place."""

    def matches(self, text: str, ctx: Context) -> bool: ...


type Pattern = str | Clause | Matcher


def matches(pattern: Pattern, text: str, ctx: Context) -> bool:
    from captain_hook.signals.nlp import scan_text

    if isinstance(pattern, Matcher):
        return pattern.matches(text, ctx)
    return scan_text(text, (pattern,))


def matches_any(patterns: Sequence[Pattern], text: str, ctx: Context) -> bool:
    return not patterns or any(matches(pattern, text, ctx) for pattern in patterns)


@dataclass(frozen=True, slots=True)
class Names:
    """Matches text that names the action's place: every non-empty value of *keys* as a whole name.

    Example:
        >>> UserSaid(Names("terminal"))
    """

    keys: tuple[str, ...] = ()

    def __init__(self, *keys: str) -> None:
        object.__setattr__(self, "keys", keys)

    def matches(self, text: str, ctx: Context) -> bool:
        values = [value for key, value in ctx.place.items() if (not self.keys or key in self.keys) and value]
        return bool(values) and all(names(text, value) for value in values)


@dataclass(frozen=True, slots=True)
class Holds:
    """Matches text that holds the action's payload field *key* verbatim, whitespace folded."""

    key: str

    def matches(self, text: str, ctx: Context) -> bool:
        value = ctx.action.payload.get(self.key)
        return isinstance(value, str) and holds(text, value)


class CustomTicketCondition(CustomCondition):
    """A condition that names what it found in the deciding declaration's :class:`Context`.

    Subclasses implement :meth:`evidence`; ``check`` holds when it finds anything.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        return self.evidence(Context.current(evt)) is not None

    def evidence(self, ctx: Context) -> list[Evidence] | None:
        """What this condition found, or ``None`` when it does not hold."""
        raise NotImplementedError


def approving(answer: Answer) -> bool:
    """Whether the user approved: picked an option other than the last one shown, or typed no hold.

    Words typed beyond a pick's number decide over the pick, so ``3, DM him`` on a three-option
    question approves while a bare ``3`` holds.
    """
    words = OPTION_NUMBER.sub("", answer.typed, count=1) if answer.chosen else answer.typed
    if words:
        return HOLD.search(words) is None
    return bool(answer.chosen) and (answer.chosen[-1] != list(answer.options)[-1] if answer.options else True)


@dataclass(frozen=True, slots=True)
class UserAnswered(CustomTicketCondition):
    """The user's answers to questions matching any of *patterns*, on ``AskUserQuestion`` or the cc-present board.

    A pattern reads the question with its options. With *approved*, only an approving answer counts: one that
    picks an option other than the last shown, or types words that hold nothing back.

    Example:
        >>> UserAnswered(Names(), approved=True)
    """

    patterns: tuple[Pattern, ...] = ()
    approved: bool = True

    def __init__(self, *patterns: Pattern, approved: bool = True) -> None:
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "approved", approved)

    def evidence(self, ctx: Context) -> list[Evidence] | None:
        return [
            item
            for item in ctx.answers
            if matches_any(self.patterns, item.detail, ctx) and (not self.approved or approving(item))
        ] or None


@dataclass(frozen=True, slots=True)
class UserRuled(CustomTicketCondition):
    """The user's durable rulings, recorded as cc-notes answers before the acting session started.

    With *key*, the rulings naming the action's value of that place key as a whole name, each carrying every
    tag in *tags*; with *ids*, the standing rulings those answer ids name, whatever action they cover.

    Example:
        >>> UserRuled("program", tags=frozenset({"scope:durable"}))
        >>> UserRuled(ids=("c9b27c1",))
    """

    key: str | None = None
    ids: tuple[str, ...] = ()
    tags: frozenset[str] = frozenset()

    def __init__(self, key: str | None = None, *, ids: Sequence[str] = (), tags: frozenset[str] = frozenset()) -> None:
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "ids", tuple(ids))
        object.__setattr__(self, "tags", tags)

    def evidence(self, ctx: Context) -> list[Evidence] | None:
        found = [item for ident in self.ids if (item := ctx.ruling(ident)) is not None]
        if self.key is not None and (term := ctx.place[self.key]):
            found += ctx.rulings(term, self.tags)
        return found or None


@dataclass(frozen=True, slots=True)
class Touched(CustomTicketCondition):
    """The tree's earlier tool calls whose input or result match any of *patterns*; by default, naming the place.

    Example:
        >>> Touched()
    """

    patterns: tuple[Pattern, ...] = ()

    def __init__(self, *patterns: Pattern) -> None:
        object.__setattr__(self, "patterns", patterns or (Names(),))

    def evidence(self, ctx: Context) -> list[Evidence] | None:
        return [
            Evidence(
                id=f"touch:{index}",
                source="touch",
                quote=touch.text[:400],
                said_at=touch.at,
                detail=f"{touch.tool} call",
            )
            for index, touch in enumerate(ctx.touches)
            if matches_any(self.patterns, touch.text, ctx)
        ] or None


def found(condition: TCondition, ctx: Context) -> list[Evidence] | None:
    """What *condition* finds in *ctx*: ``None`` when it does not hold, else the evidence its parts contribute."""
    match condition:
        case And(conditions):
            parts = [found(part, ctx) for part in conditions]
            return None if any(part is None for part in parts) else [item for part in parts if part for item in part]
        case Or(conditions):
            return next((part for sub in conditions if (part := found(sub, ctx)) is not None), None)
        case Not(sub):
            return None if found(sub, ctx) is not None else []
        case UserSaid(patterns=patterns):
            return [item for item in ctx.said if matches_any(patterns, item.quote, ctx)] or None
        case CustomTicketCondition():
            return condition.evidence(ctx)
        case _:
            if ctx.evt is None:
                raise TypeError(f"{type(condition).__name__} reads an event, and this context has none")
            return [] if check_condition(condition, ctx.evt) else None
