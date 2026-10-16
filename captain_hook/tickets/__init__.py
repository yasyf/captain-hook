"""Tickets: a declarative set of rules that let an action go ahead, and the record that one did."""

from __future__ import annotations

from captain_hook.tickets.conditions import (
    CustomTicketCondition,
    Holds,
    Matcher,
    Names,
    Pattern,
    Touched,
    UserAnswered,
    UserRuled,
    found,
)
from captain_hook.tickets.context import Context, Touch, holds, names, tree_of
from captain_hook.tickets.decide import Decide, Undecided, Verdict
from captain_hook.tickets.declare import Rule, Tickets, lifted
from captain_hook.tickets.records import Allow, Answer, Deny, Evidence, Proposal, Reach, Ticket, Use

__all__ = [
    "Proposal",
    "Allow",
    "Answer",
    "Context",
    "CustomTicketCondition",
    "Decide",
    "Deny",
    "Evidence",
    "Holds",
    "Matcher",
    "Names",
    "Pattern",
    "Reach",
    "Rule",
    "Ticket",
    "Tickets",
    "Touch",
    "Touched",
    "Undecided",
    "Use",
    "UserAnswered",
    "UserRuled",
    "Verdict",
    "found",
    "holds",
    "lifted",
    "names",
    "tree_of",
]
