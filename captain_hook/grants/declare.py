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
from captain_hook.grants.records import Allowed, Denied, Evidence, Grant, Proposal

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

    def grant(
        self,
        evt: BaseHookEvent,
        *,
        scope: dict[str, str],
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
                scope=self.canonical(scope),
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
            return Denied(f"the grant check failed ({type(exc).__name__}: {exc}).", self.would_allow)

    def check(self, evt: BaseHookEvent, action: Proposal | None = None) -> Allowed | Denied:
        """Spend a grant that covers *action*, minting one from the owner's words when none is stored.

        *action* defaults to the declaration's ``action`` of *evt*. Stored grants of this kind in the
        session tree come first, newest first: a rule that denies skips the grant, live evidence the
        sources no longer collect skips it, a rule that allows settles it unless the owner has spoken
        since it was minted, and anything else goes to the judge with the grant's evidence and the
        owner's later words. With no stored grant, the judge reads the declared evidence; its allow
        mints a grant keyed on the approval it relied on and spends it. Unlimited standing words and
        rulings permit a class of actions, so they mint one standing grant per destination; a counted
        approval keeps one budget, and any other approval covers one destination. Every use is reserved
        and settles with the event's verdict.
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

        refusals: list[str] = []
        found = store.matching(self.kind, tree, scope, fingerprint(action), self.replay)
        refusals.extend(why for _, why in found[:1] if why is not None)
        for grant in (grant for grant, why in found if why is None):
            rulings = [rule.evaluate(grant, action) for rule in self.applicable(grant)]
            if denied := next((ruling for ruling in rulings if ruling.verdict == "deny"), None):
                refusals.append(f"grant {grant.id}: {denied.note}")
                continue
            if any(item.live for item in grant.evidence) and (stale := self.stale(grant, session())):
                refusals.append(f"grant {grant.id} rests on {stale}, which changed after the grant was minted.")
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
                    refusals.append(f"grant {grant.id}: {verdict.reason}")
                    continue
                reason, relied = verdict.reason, verdict.relied_on
            try:
                return self.spend(evt, grant, action, reason, relied)
            except store.SpentError as exc:
                refusals.append(str(exc))
        if not self.evidence:
            return Denied(" ".join(refusals) or f"No {self.kind} grant covers {action.summary}.", self.would_allow)
        return self.from_evidence(evt, action, session(), refusals)

    def from_evidence(
        self, evt: BaseHookEvent, action: Proposal, items: list[Evidence], refusals: list[str]
    ) -> Allowed | Denied:
        if self.judge is None:
            if not items:
                return Denied(" ".join(refusals), self.would_allow)
            named = items[0]
            verdict = GrantVerdict(
                reason=f"{named.detail or named.id} covers {action.summary}", allow=True, relied_on=[named.id]
            )
        else:
            try:
                verdict = self.judge(evt, hook=self.hook, action=action, evidence=items, rulings=())
            except JudgeFailed as exc:
                return Denied(
                    f"{exc}, and an action it cannot judge never goes ahead.", self.would_allow, undecided=True
                )
        if not verdict.allow:
            return Denied(" ".join([*refusals, verdict.reason]), self.would_allow)
        relied = cited(items, verdict.relied_on)
        if not relied:
            return Denied(
                " ".join([*refusals, "The judge allowed without citing any of the owner's words."]), self.would_allow
            )
        owners = [item for item in items if item.source in OWNER_SOURCES]
        destination = f"#{json.dumps(self.canonical(action))}"
        per_destination = destination if verdict.uses is None else ""
        ruling = next((item for item in relied if item.source in RULING_SOURCES), None)
        if verdict.standing and (said := verbatim(verdict.standing, owners)) is not None:
            quoted = said.model_copy(update={"quote": verdict.standing, "detail": said.quote})
            grant = self.grant(
                evt,
                scope=dict(action.scope),
                evidence=[quoted, *(item for item in relied if item.id != said.id)],
                uses=verdict.uses,
                ttl=self.standing_ttl,
                rules=self.standing_rules,
                source_key=f"{said.key or said.id}{per_destination}",
            )
        elif ruling is not None and self.judge is not None:
            grant = self.grant(
                evt,
                scope=dict(action.scope),
                evidence=relied,
                uses=verdict.uses,
                ttl=self.standing_ttl,
                rules=self.standing_rules,
                source_key=f"{ruling.key or ruling.id}{per_destination}",
            )
        else:
            approval = relied[0].key or relied[0].id
            grant = self.grant(
                evt,
                scope=dict(action.scope),
                evidence=relied,
                uses=self.mint,
                ttl=self.ttl,
                approved=dict(action.payload) if action.payload else None,
                source_key=approval if self.judge is not None else f"{approval}{destination}",
                across_trees=self.judge is None,
            )
        if grant.scope != self.canonical(action):
            return Denied(
                " ".join([*refusals, f"The approval it relied on already covers {grant.scope} as grant {grant.id}."]),
                self.would_allow,
            )
        rulings = [rule.evaluate(grant, action) for rule in self.applicable(grant)]
        if denied := next((ruling for ruling in rulings if ruling.verdict == "deny"), None):
            return Denied(f"grant {grant.id}: {denied.note}", self.would_allow)
        try:
            return self.spend(evt, grant, action, verdict.reason, verdict.relied_on)
        except store.SpentError as exc:
            return Denied(" ".join([*refusals, str(exc)]), self.would_allow)

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

    The check fails closed: a store, evidence, or judge error keeps the block. The block's own message
    stays the agent-facing text; why no grant covered the call goes to the user as ``system_message``.
    """
    verdict = grants.decide(evt)
    if isinstance(verdict, Denied):
        return replace(result, system_message=f"{hook}: {verdict.message}")
    left = "unlimited uses" if verdict.remaining is None else f"{verdict.remaining} use(s) left"
    return evt.context(f"{hook}: allowed by grant `{verdict.grant.id}`, {left}.")
