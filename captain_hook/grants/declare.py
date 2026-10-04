"""``Grants``: the declaration a hook makes, and the one ``check`` that spends a grant or says why not."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.grants import store
from captain_hook.grants.evidence import Asked, OwnerWords, lapsed, tree_of, verbatim
from captain_hook.grants.judge import GrantVerdict, Judge, JudgeFailed
from captain_hook.grants.orca import adopt_coordinator
from captain_hook.grants.records import (
    Allowed,
    Denied,
    Evidence,
    Grant,
    Proposal,
    ScopeValue,
    covers,
    render_scope,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.grants.evidence import EvidenceSource
    from captain_hook.grants.rules import Rule
    from captain_hook.types import HookResult

OWNER_SOURCES = frozenset({"ask", "words"})
RULING_SOURCES = frozenset({"ccn-answer"})
DECLARED: dict[str, Grants] = {}
_RESERVED: ContextVar[list[str] | None] = ContextVar("captain_hook_grant_reservations", default=None)


@contextmanager
def reservations() -> Generator[list[str]]:
    """Hold the uses every check in this dispatch reserves, so the dispatch settles them with its verdict."""
    reserved: list[str] = []
    token = _RESERVED.set(reserved)
    try:
        yield reserved
    finally:
        _RESERVED.reset(token)


def settle(reserved: Sequence[str], *, allowed: bool) -> bool:
    """Settle this dispatch's reservations with its verdict; ``False`` when an allowed call lost its use."""
    return all([store.settle(tool_use_id, allowed=allowed) for tool_use_id in dict.fromkeys(reserved)])


def call_id(evt: BaseHookEvent) -> str:
    return evt.tool_use_id or evt.__dict__.setdefault("_grant_call_id", f"call-{uuid.uuid4().hex[:12]}")


def fingerprint(action: Proposal) -> str:
    material = json.dumps([dict(action.scope), dict(action.payload)], sort_keys=True, default=str)
    return sha256(material.encode()).hexdigest()[:16]


def cited(items: Sequence[Evidence], ids: Sequence[str]) -> list[Evidence]:
    named = {ident.strip().strip("[]") for ident in ids}
    return [item for item in items if item.id in named or item.id.partition(":")[2] in named]


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why one grant or approval did not cover an action: plainly for the agent, in full for the user."""

    agent: str
    detail: str


def unusable_refusal(why: store.Unusable) -> Refusal:
    return Refusal(why.agent, why.detail)


def scope_key(scope: dict[str, ScopeValue]) -> str:
    return f"#{json.dumps(scope, sort_keys=True)}"


def author(evt: BaseHookEvent, hook: str) -> str:
    return f"hook:{hook}@{evt.session_id}/{evt.agent_id or 'main'}"


@dataclass(frozen=True, slots=True)
class Grants:
    """A hook's declaration of the spendable permission it accepts.

    Attributes:
        kind: The grant kind this declaration spends and mints, e.g. ``slack.write``.
        scope: The canonical scope keys; every action and grant carries exactly these.
        action: Maps the event to the action a grant must cover; a hook that passes each action to
            :meth:`check` itself, as a guard does for every call in one command, declares none.
        rules: Deterministic rules run against each candidate grant before any judge.
        judge: The LLM check over the owner's words. Without one, a scope match is enough for a
            stored grant, and the first evidence item collected is the approval itself, minted once
            per scope across every session tree, so a judge-less declaration reads only sources that
            name the action, such as ``Rulings``.
        evidence: Where the owner's words live, read when no stored grant covers the action.
        mint: Uses of a grant minted from session evidence; ``None`` mints a grant every later action may spend.
        ttl: How long a grant minted from session evidence lives.
        standing_ttl: How long a standing grant minted from the owner's verbatim words lives.
        standing_rules: Rule names a standing grant asserts.
        widen: Scope keys the judge may widen past the action's own value when the owner's words cover
            more, to a set of named values or ``*`` for every value; standing words for every thread of
            a channel widen ``thread``.
        replay: How long after a one-use grant's spend the identical action goes again without a second
            use, for a retry whose result was lost; keep it short when the effect does not dedupe.
        spent_by: The downstream system that spends this kind's grants with ``capt-hook grant spend``;
            when set, a check names the covering grant without reserving a use.
        would_allow: What the agent can do to get permission, appended to every deny.
    """

    kind: str
    scope: tuple[str, ...]
    action: Callable[[BaseHookEvent], Proposal] | None = None
    rules: Sequence[Rule] = ()
    judge: Judge | None = None
    evidence: Sequence[EvidenceSource] = ()
    mint: int | None = 1
    ttl: timedelta | None = timedelta(days=1)
    standing_ttl: timedelta | None = None
    standing_rules: tuple[str, ...] = ()
    widen: tuple[str, ...] = ()
    replay: timedelta = timedelta.max
    spent_by: str | None = None
    would_allow: str = "Ask the user for permission for exactly this action."
    hook: str = field(default="grants")

    def __post_init__(self) -> None:
        if self.judge is None and any(isinstance(source, Asked | OwnerWords) for source in self.evidence):
            raise ValueError(f"{self.kind}: the owner's words and answers need a judge to read them")
        DECLARED[self.kind] = self

    def canonical(self, scope: dict[str, str] | Proposal) -> dict[str, str]:
        values = dict(scope.scope if isinstance(scope, Proposal) else scope)
        if set(values) != set(self.scope):
            raise ValueError(f"{self.kind} scopes carry exactly {sorted(self.scope)}, got {sorted(values)}")
        return {key: str(values[key]) for key in self.scope}

    def pattern(self, scope: dict[str, ScopeValue]) -> dict[str, ScopeValue]:
        """*scope* with this declaration's keys, each an exact value, ``*``, or a sorted set."""
        if set(scope) != set(self.scope):
            raise ValueError(f"{self.kind} scopes carry exactly {sorted(self.scope)}, got {sorted(scope)}")
        return {
            key: sorted({str(item) for item in value}) if isinstance(value, list) else str(value)
            for key, value in ((key, scope[key]) for key in self.scope)
        }

    def widened(self, action: Proposal, verdict: GrantVerdict) -> dict[str, ScopeValue] | None:
        """The scope a grant minted on *verdict* covers, or ``None`` when the judge widened past what it may.

        Keys the declaration lists in ``widen`` take the judge's values; any other key keeps the action's.
        The result must still cover the action itself.
        """
        own = self.canonical(action)
        scope: dict[str, ScopeValue] = dict(own)
        for key, value in (verdict.scope or {}).items():
            if key not in self.scope or (key not in self.widen and value != own[key]):
                return None
            scope[key] = value
        minted = self.pattern(scope)
        return minted if covers(minted, own) else None

    def grant(
        self,
        evt: BaseHookEvent,
        *,
        scope: dict[str, ScopeValue],
        evidence: Sequence[Evidence],
        uses: int | None = 1,
        ttl: timedelta | None = None,
        approved: dict[str, object] | None = None,
        rules: Sequence[str] = (),
        source_key: str | None = None,
        links: dict[str, str] | None = None,
        across_trees: bool = False,
    ) -> Grant:
        """Mint a grant in *evt*'s session tree, or return the one already minted from *source_key*.

        With *across_trees*, the grant already minted from *source_key* in any tree is returned.
        """
        return store.mint(
            Grant(
                id=store.new_id(),
                kind=self.kind,
                tree=tree_of(evt),
                scope=self.pattern(scope),
                approved=approved,
                uses=uses,
                expires=store.expiry(ttl),
                rules=list(rules),
                evidence=list(evidence),
                source_key=source_key,
                links=links or {},
                author=author(evt, self.hook),
                created=store.now(),
            ),
            across_trees=across_trees,
        )

    def stale(self, grant: Grant, items: Sequence[Evidence]) -> str | None:
        current = {item.key for item in items if item.live}
        return next((item.id for item in grant.evidence if item.live and item.key not in current), None)

    def applicable(self, grant: Grant) -> list[Rule]:
        return [rule for rule in self.rules if rule.always or rule.name in grant.rules]

    def decide(self, evt: BaseHookEvent, action: Proposal | None = None) -> Allowed | Denied:
        """:meth:`check`, failing closed: a store, evidence, or judge error is a denial naming the error."""
        try:
            return self.check(evt, action)
        except Exception as exc:
            logger.bind(hook=self.hook, kind=self.kind).opt(exception=True).warning("grant check failed")
            return Denied(f"The grant check failed ({type(exc).__name__}: {exc}).", self.would_allow)

    def check(self, evt: BaseHookEvent, action: Proposal | None = None) -> Allowed | Denied:
        """Spend a grant that covers *action*, minting one from the owner's words when none is stored.

        *action* defaults to the declaration's ``action`` of *evt*. Stored grants of this kind in the
        session tree whose scope covers the action come first, newest first: a rule that denies skips the
        grant, live evidence the sources no longer collect skips it, a rule that allows settles it unless
        the owner has spoken since it was minted, and anything else goes to the judge with the grant's
        evidence and the owner's later words. With no stored grant, the judge reads the declared evidence;
        its allow mints a grant keyed on the approval it relied on and spends it. Standing words and
        rulings permit a class of actions, so they mint a standing grant per scope the judge reads them as
        covering, widened on the declaration's ``widen`` keys; a counted approval keeps one budget, and any
        other approval covers one scope. Every use is reserved and settles with the event's verdict.
        """
        if action is None:
            if self.action is None:
                raise TypeError(f"{self.kind} declares no action, so check needs the action to cover")
            action = self.action(evt)
        scope = self.canonical(action)
        tree = tree_of(evt)
        adopt_coordinator(evt)
        collected: list[Evidence] | None = None

        def session() -> list[Evidence]:
            nonlocal collected
            if collected is None:
                collected = [item for source in self.evidence for item in source.collect(evt, action)]
            return collected

        refusals: list[Refusal] = []
        found = store.matching(self.kind, tree, scope, fingerprint(action), self.replay)
        refusals.extend(unusable_refusal(why) for _, why in found[:1] if why is not None)
        for grant in (grant for grant, why in found if why is None):
            rulings = [rule.evaluate(grant, action) for rule in self.applicable(grant)]
            if denied := next((ruling for ruling in rulings if ruling.verdict == "deny"), None):
                refusals.append(
                    Refusal(
                        f"A standing permission never covers this: {denied.note}.", f"grant {grant.id}: {denied.note}."
                    )
                )
                continue
            if any(item.live for item in grant.evidence) and (stale := self.stale(grant, session())):
                refusals.append(
                    Refusal(
                        "The record the permission rests on changed after it was granted.",
                        f"Grant {grant.id} rests on {stale}, which changed after the grant was minted.",
                    )
                )
                continue
            reason, relied = f"covered by grant {grant.id}", [item.id for item in grant.evidence]
            later = [*session(), *lapsed(evt, grant.created)] if self.judge is not None else []
            since = sorted(
                {
                    item.id: item
                    for item in later
                    if item.source in OWNER_SOURCES and item.said_at is not None and item.said_at > grant.created
                }.values(),
                key=lambda item: item.said_at.timestamp() if item.said_at else 0.0,
            )
            allowed_by_rule = any(ruling.verdict == "allow" for ruling in rulings)
            if self.judge is not None and (since or not allowed_by_rule):
                try:
                    verdict = self.judge(
                        evt, hook=self.hook, action=action, evidence=since, rulings=rulings, grant=grant
                    )
                except JudgeFailed as exc:
                    return Denied(
                        f"{exc}, and a grant it cannot judge never covers an action.", self.would_allow, undecided=True
                    )
                if verdict.withdrawn:
                    store.revoke(grant.id)
                if not verdict.allow:
                    refusals.append(Refusal(verdict.explained, f"grant {grant.id}: {verdict.reason}"))
                    continue
                reason, relied = verdict.reason, verdict.relied_on
            try:
                return self.spend(evt, grant, action, reason, relied)
            except store.SpentError as exc:
                refusals.append(unusable_refusal(exc.why))
        if not self.evidence:
            if not refusals:
                return Denied(f"No {self.kind} grant covers {action.summary}.", self.would_allow)
            return Denied(refusals[-1].agent, self.would_allow, detail=" ".join(why.detail for why in refusals))
        return self.from_evidence(evt, action, session(), refusals)

    def from_evidence(
        self,
        evt: BaseHookEvent,
        action: Proposal,
        items: list[Evidence],
        refusals: list[Refusal],
    ) -> Allowed | Denied:
        def denied(*why: Refusal) -> Denied:
            reasons = [*refusals, *why]
            if not reasons:
                return Denied("", self.would_allow)
            return Denied(reasons[-1].agent, self.would_allow, detail=" ".join(reason.detail for reason in reasons))

        if not items:
            return denied()
        if self.judge is None:
            named = items[0]
            verdict = GrantVerdict(
                reason=f"{named.detail or named.id} covers {action.summary}", allow=True, relied_on=[named.id]
            )
        else:
            try:
                verdict = self.judge(evt, hook=self.hook, action=action, evidence=items, rulings=(), widen=self.widen)
            except JudgeFailed as exc:
                return Denied(
                    f"{exc}, and an action it cannot judge never goes ahead.", self.would_allow, undecided=True
                )
        if not verdict.allow:
            return denied(Refusal(verdict.explained, verdict.reason))
        relied = cited(items, verdict.relied_on)
        if not relied:
            uncited = "The judge allowed this without citing any of the owner's words, so the allow does not count."
            return denied(Refusal(uncited, f"{uncited} It cited {verdict.relied_on}."))
        if (scope := self.widened(action, verdict)) is None:
            return denied(
                Refusal(
                    "The owner's words do not reach as far as the judge read them.",
                    f"The judge read the owner's words as covering {render_scope(verdict.scope or {})}, which a"
                    f" {self.kind} grant cannot widen to from this action.",
                )
            )
        owners = [item for item in items if item.source in OWNER_SOURCES]
        ruling = next((item for item in relied if item.source in RULING_SOURCES), None)
        said = verbatim(verdict.standing, owners) if verdict.standing else None
        basis = said or (ruling if self.judge is not None else None)
        if basis is not None:
            evidence = relied
            if said is not None:
                quoted = said.model_copy(update={"quote": verdict.standing, "detail": said.quote})
                evidence = [quoted, *(item for item in relied if item.id != said.id)]
            grant = self.grant(
                evt,
                scope=scope,
                evidence=evidence,
                uses=verdict.uses,
                ttl=self.standing_ttl,
                rules=self.standing_rules,
                source_key=f"{basis.key or basis.id}{scope_key(scope) if verdict.uses is None else ''}",
            )
        else:
            approval = relied[0].key or relied[0].id
            grant = self.grant(
                evt,
                scope=scope,
                evidence=relied,
                uses=verdict.uses or self.mint,
                ttl=self.ttl,
                approved=dict(action.payload) if action.payload and scope == self.canonical(action) else None,
                source_key=approval if self.judge is not None else f"{approval}{scope_key(scope)}",
                across_trees=self.judge is None,
            )
        if not covers(grant.scope, self.canonical(action)):
            return denied(
                Refusal(
                    f"The owner's approval already covers {render_scope(grant.scope)}, not this action.",
                    f"The owner's approval {relied[0].id} already went to grant {grant.id} for"
                    f" {render_scope(grant.scope)}, which does not cover"
                    f" {action.summary or render_scope(action.scope)}.",
                )
            )
        rulings = [rule.evaluate(grant, action) for rule in self.applicable(grant)]
        if rule_denied := next((ruling for ruling in rulings if ruling.verdict == "deny"), None):
            return denied(
                Refusal(
                    f"A standing permission never covers this: {rule_denied.note}.",
                    f"grant {grant.id}: {rule_denied.note}.",
                )
            )
        try:
            return self.spend(evt, grant, action, verdict.reason, verdict.relied_on)
        except store.SpentError as exc:
            return denied(unusable_refusal(exc.why))

    def request(
        self,
        evt: BaseHookEvent,
        *,
        scope: dict[str, ScopeValue],
        quote: str,
        uses: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> Allowed | Denied:
        """Record the grant an agent asks for on the owner's behalf, without spending it.

        *quote* must sit verbatim in the owner's own words, and the judge must read those words as
        permitting every action *scope* admits, *uses* times or without limit. The grant rests on the
        quoted words and expires with ``standing_ttl``; asking again for the same words and scope returns
        the grant already recorded.
        """
        if self.judge is None:
            raise TypeError(f"{self.kind} declares no judge, so it records no grant an agent asks for")
        requested = self.pattern(scope)
        summary = f"record a {self.kind} grant for {render_scope(requested)}"
        action = Proposal(
            scope={key: json.dumps(value) if isinstance(value, list) else value for key, value in requested.items()},
            payload={"quote": quote, "uses": uses, **(payload or {})},
            summary=summary,
        )
        items = [item for source in self.evidence for item in source.collect(evt, action)]
        said = verbatim(quote, [item for item in items if item.source in OWNER_SOURCES])
        if said is None:
            return Denied(f"The quote for {summary} is not verbatim in the owner's own words.", self.would_allow)
        try:
            verdict = self.judge(evt, hook=self.hook, action=action, evidence=items, rulings=())
        except JudgeFailed as exc:
            return Denied(f"{exc}, and a grant it cannot judge is never recorded.", self.would_allow, undecided=True)
        if not verdict.allow:
            return Denied(verdict.explained, self.would_allow, detail=verdict.reason)
        grant = self.grant(
            evt,
            scope=requested,
            evidence=[said.model_copy(update={"quote": quote, "detail": said.quote})],
            uses=uses,
            ttl=self.standing_ttl,
            rules=self.standing_rules,
            source_key=f"request:{said.key or said.id}{scope_key(requested)}",
        )
        return Allowed(grant, store.remaining(grant, store.spends(grant.id), store.now()), verdict.reason)

    def spend(self, evt: BaseHookEvent, grant: Grant, action: Proposal, reason: str, relied: list[str]) -> Allowed:
        if self.spent_by is not None:
            at = store.now()
            used = store.spends(grant.id)
            if (why := store.unusable(grant, used, at, fingerprint(action))) is not None:
                raise store.SpentError(why)
            return Allowed(grant, store.remaining(grant, used, at), reason)
        reserved = _RESERVED.get()
        tool_use_id = call_id(evt)
        left = store.reserve(
            grant.id,
            tree=tree_of(evt),
            scope=self.canonical(action),
            state="reserved" if reserved is not None else "committed",
            session=evt.session_id,
            agent=evt.agent_id or "main",
            tool_use_id=tool_use_id,
            fingerprint=fingerprint(action),
            summary=action.summary,
            reason=reason,
            relied_on=relied,
            replay=self.replay,
        )
        if reserved is not None:
            reserved.append(tool_use_id)
        logger.bind(hook=self.hook, grant=grant.id, remaining=left, reason=reason).info("grant spent")
        return Allowed(grant, left, reason)


def lifted(evt: BaseHookEvent, hook: str, result: HookResult, grants: Grants) -> HookResult:
    """Settle the block of a hook declared with ``grants=``: a covering grant lifts it, anything else keeps it.

    The check fails closed: a store, evidence, or judge error keeps the block. When the check says why no
    grant covered the call, the block's message becomes that reason and what would allow it, in two
    sentences; every refusal and the judge's full reasoning go to the user as ``system_message``.
    """
    verdict = grants.decide(evt)
    if isinstance(verdict, Denied):
        return refused(result, hook, verdict)
    left = "unlimited uses" if verdict.remaining is None else f"{verdict.remaining} use(s) left"
    return evt.context(f"{hook}: allowed by grant `{verdict.grant.id}`, {left}.")


def refused(result: HookResult, hook: str, verdict: Denied) -> HookResult:
    """*result*'s block saying why no grant covered the call, with the full reasoning for the user."""
    message = verdict.message if verdict.reason else result.message
    return replace(result, message=message, system_message=f"{hook}: {verdict.explained}")
