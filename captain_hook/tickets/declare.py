"""``Tickets``: the declaration a hook makes, and the one ``decide`` that finds a ticket or mints one."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from captain_hook.tickets import store
from captain_hook.tickets.conditions import found
from captain_hook.tickets.context import Context
from captain_hook.tickets.decide import Undecided, decision_log, decision_stage
from captain_hook.tickets.orca import coordinator_tree
from captain_hook.tickets.records import Allow, Deny, Evidence, Proposal, Reach, Ticket

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult, TCondition

DECLARED: dict[str, Tickets] = {}
UNCHECKED = "unchecked"
UNCHECKED_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Rule:
    """One way an action gets a ticket: the condition that, holding, supplies the ticket's evidence."""

    name: str
    when: TCondition


@dataclass(frozen=True, slots=True)
class Tickets:
    """A hook's declaration of the tickets it accepts and the rules that mint them.

    Attributes:
        kind: The ticket kind this declaration finds and mints, e.g. ``slack.write``.
        place: The place keys; every action and ticket carries exactly these.
        reach: How far a minted ticket reaches: this one action, or every later action at its place.
        rules: The rules that mint a ticket, in order; the first whose condition holds mints and allows.
        deny: What the agent can do to get permission, said on every deny.
        action: Maps the event to the action to cover; a hook that passes each action to :meth:`check`
            itself declares none.
        ttl: How long a minted ticket lives.
        window: How many recent messages of each session the context reads.
        machine: Markers of a prompt a tool wrote on the user's behalf; such a prompt is never their words.
        hook: The hook the declaration belongs to, for logs.
        own_session: Whether tickets live in the acting session's own tree, for a place that names the acting
            agent: that tree holds while the session joins and leaves an Orca coordinator's tree.
    """

    kind: str
    place: tuple[str, ...]
    reach: Reach
    rules: tuple[Rule, ...]
    deny: str
    action: Callable[[BaseHookEvent], Proposal] | None = None
    ttl: timedelta | None = timedelta(days=7)
    window: int = 60
    machine: tuple[str, ...] = ()
    hook: str = "tickets"
    own_session: bool = False

    def __post_init__(self) -> None:
        DECLARED[self.kind] = self

    def canonical(self, action: Proposal) -> Proposal:
        if set(action.place) != set(self.place):
            raise ValueError(f"{self.kind} places carry exactly {sorted(self.place)}, got {sorted(action.place)}")
        return replace(action, place={key: str(action.place[key]) for key in self.place})

    def context(self, evt: BaseHookEvent, action: Proposal | None = None) -> Context:
        """The context :meth:`decide` reads for *evt*: *action*, or the declaration's ``action`` of the event."""
        if action is None:
            if self.action is None:
                raise TypeError(f"{self.kind} declares no action, so the context needs the action to cover")
            action = self.action(evt)
        coordinator = coordinator_tree(evt)
        return Context.of(
            evt,
            self.kind,
            self.canonical(action),
            tree=evt.session_id if self.own_session else None,
            trees=() if coordinator is None else (coordinator,),
            window=self.window,
            machine=self.machine,
        )

    def mint(self, ctx: Context, rule: str, evidence: Sequence[Evidence]) -> Ticket:
        """Mint the ticket *rule* earned for *ctx*'s action."""
        created = store.now()
        return store.mint(
            Ticket(
                id=store.new_id(),
                kind=self.kind,
                tree=ctx.tree,
                place=ctx.place,
                reach=self.reach,
                rule=rule,
                evidence=list(evidence),
                fingerprint=ctx.action.fingerprint if self.reach is Reach.ONCE else None,
                author=f"hook:{self.hook}@{ctx.session}/{ctx.agent}",
                created=created,
                expires=None if self.ttl is None else created + self.ttl,
            )
        )

    def unchecked(self, ctx: Context, cause: str) -> Ticket:
        """Mint the once ticket that lets *ctx*'s action go ahead when the check itself could not finish.

        It records *cause* as its evidence and covers this one action for :data:`UNCHECKED_TTL`, so the
        store says why nothing checked the action and a later action at the place still gets checked.
        """
        created = store.now()
        ticket = store.mint(
            Ticket(
                id=store.new_id(),
                kind=self.kind,
                tree=ctx.tree,
                place=ctx.place,
                reach=Reach.ONCE,
                rule=UNCHECKED,
                evidence=[
                    Evidence(
                        id=f"{UNCHECKED}:{ctx.call}",
                        source="hook",
                        quote=cause,
                        said_at=created,
                        detail="the check did not finish, so the action went ahead unchecked",
                    )
                ],
                fingerprint=ctx.action.fingerprint,
                author=f"hook:{self.hook}@{ctx.session}/{ctx.agent}",
                created=created,
                expires=created + UNCHECKED_TTL,
            )
        )
        decision_log().bind(hook=self.hook, kind=self.kind, ticket=ticket.id, cause=cause).warning("unchecked ticket minted")
        return ticket

    def decide(self, ctx: Context) -> Allow | Deny:
        """Find the live ticket covering *ctx*'s action, else mint one from the first rule that holds, else deny."""
        action = ctx.action
        if (ticket := store.covering(self.kind, ctx.trees, action.place, action.fingerprint)) is not None:
            decision_log().bind(hook=self.hook, kind=self.kind, ticket=ticket.id).info("ticket found")
            return Allow(ticket, minted=False)
        passed: list[str] = []
        for rule in self.rules:
            try:
                with decision_stage(ctx, f"rule:{rule.name}"):
                    evidence = found(rule.when, ctx)
            except Undecided as exc:
                passed.append(f"{rule.name}: {exc}")
                continue
            if evidence is None:
                passed.append(f"{rule.name}: nothing found")
                continue
            ticket = self.mint(ctx, rule.name, evidence)
            decision_log().bind(hook=self.hook, kind=self.kind, rule=rule.name, ticket=ticket.id).info("ticket minted")
            return Allow(ticket, minted=True)
        return Deny(self.deny, "; ".join(passed))

    def check(self, evt: BaseHookEvent, action: Proposal | None = None) -> Allow | Deny:
        """:meth:`decide` over :meth:`context`, failing closed: a store, source, or judge error denies, naming it."""
        try:
            return self.decide(self.context(evt, action))
        except Exception as exc:
            decision_log().bind(hook=self.hook, kind=self.kind).opt(exception=True).warning("ticket check failed")
            return Deny(self.deny, f"The ticket check failed ({type(exc).__name__}: {exc}).")


def lifted(evt: BaseHookEvent, hook: str, result: HookResult, tickets: Tickets) -> HookResult:
    """Settle the block of a hook declared with ``tickets=``: a covering ticket lifts it, anything else keeps it."""
    verdict = tickets.check(evt)
    if isinstance(verdict, Deny):
        return refused(result, hook, verdict)
    return evt.context(f"{hook}: allowed by ticket `{verdict.ticket.id}` ({verdict.ticket.rule}).")


def refused(result: HookResult, hook: str, verdict: Deny) -> HookResult:
    """*result*'s block saying what would allow the call, with every rule's finding for the user."""
    return replace(
        result, message=verdict.message, system_message=f"{hook}: {verdict.detail}" if verdict.detail else None
    )
