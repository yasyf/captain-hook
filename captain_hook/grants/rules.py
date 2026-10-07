"""Deterministic grant rules: each settles a call outright or leaves it to the judge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, Protocol

from captain_hook.grants.records import Grant, Proposal

RulingVerdict = Literal["allow", "deny", "defer"]


@dataclass(frozen=True, slots=True)
class Ruling:
    """One rule's result: ``allow`` and ``deny`` settle the call, ``defer`` leaves it to the judge."""

    rule: str
    verdict: RulingVerdict
    note: str

    def line(self) -> str:
        return f"{self.rule}: {self.verdict} ({self.note})"


class Rule(Protocol):
    """A deterministic predicate over a grant and the action it would cover.

    ``always`` rules apply to every grant of the declaration; the rest apply only to grants that
    name them in :attr:`~captain_hook.grants.Grant.rules`.
    """

    name: str
    always: bool

    def evaluate(self, grant: Grant, action: Proposal) -> Ruling: ...


def word_diff(approved: str, pending: str) -> str:
    """A word-level ``-``/``+`` diff of *pending* against *approved*."""
    a, b = approved.split(), pending.split()
    hunks: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
        if tag in ("delete", "replace"):
            hunks.append("- " + " ".join(a[i1:i2]))
        if tag in ("insert", "replace"):
            hunks.append("+ " + " ".join(b[j1:j2]))
    return "\n".join(hunks) or "(whitespace only)"


@dataclass(frozen=True, slots=True)
class ContentMatches:
    """Allow when the action's payload is the payload the owner approved.

    Every key of the grant's ``approved`` payload must equal the action's; *text* keys compare
    through *equivalent*, so a hook can accept differences it knows carry no meaning (a mention
    rendered as an id, a link wrapped). Any other difference denies with a word diff, since an approved
    payload covers only itself.
    """

    text: tuple[str, ...] = ("text",)
    equivalent: Callable[[str, str], bool] = str.__eq__
    name: str = "content"
    always: bool = True

    def evaluate(self, grant: Grant, action: Proposal) -> Ruling:
        if grant.approved is None:
            return Ruling(self.name, "defer", "the grant names no approved payload")
        differences: list[str] = []
        for key in sorted(set(grant.approved) | set(action.payload)):
            approved, pending = grant.approved.get(key), action.payload.get(key)
            if key in self.text and isinstance(approved, str) and isinstance(pending, str):
                if not self.equivalent(approved, pending):
                    differences.append(f"{key}:\n{word_diff(approved, pending)}")
            elif approved != pending:
                differences.append(f"{key}: approved {approved!r}, pending {pending!r}")
        if not differences:
            return Ruling(self.name, "allow", "the payload is the one the owner approved")
        return Ruling(self.name, "deny", "it differs from the approved payload:\n" + "\n".join(differences))


@dataclass(frozen=True, slots=True)
class Never:
    """Deny every action *test* matches, saying *message*; apply it to grants that name it unless *always*."""

    name: str
    test: Callable[[Proposal], bool]
    message: str
    always: bool = False

    def evaluate(self, grant: Grant, action: Proposal) -> Ruling:
        return Ruling(self.name, "deny", self.message) if self.test(action) else Ruling(self.name, "defer", "holds")
