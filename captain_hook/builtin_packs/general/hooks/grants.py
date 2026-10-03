from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, CommandSchema, Event, Input, LambdaCondition, Operand, Option, Tool, on
from captain_hook.grants import store
from captain_hook.grants.declare import DECLARED
from captain_hook.grants.evidence import Asked, OwnerWords, answer_evidence, parse_answer, tree_of, verbatim
from captain_hook.grants.records import Grant, Proposal

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent, Call, HookResult, PostToolUseEvent

ASK_TTL = timedelta(days=1)
MINTING = frozenset({"add", "import"})
GRANT_CLI = CommandSchema(
    "capt-hook",
    operands=(Operand("verbs", count=2), Operand("rest", count="*")),
    options=(
        Option("kind", ("--kind",)),
        Option("scope", ("--scope",)),
        Option("uses", ("--uses",)),
        Option("unlimited", ("--unlimited",), bool),
        Option("span", ("--for",)),
        Option("rule", ("--rule",)),
        Option("tree", ("--tree",)),
        Option("quote", ("--quote",)),
    ),
)


@on(
    Event.PostToolUse,
    only_if=[Tool("AskUserQuestion")],
    respect_gitignore=False,
    skip_planning_agents=False,
)
def record_answers(evt: PostToolUseEvent) -> HookResult | None:
    payload = evt.tool_response
    if not isinstance(payload, dict) or (answer := parse_answer(payload)) is None or evt.tool_use_id is None:
        return None
    for item in answer_evidence(evt.tool_use_id, payload, answer, store.now()):
        store.mint(
            Grant(
                id=store.new_id(),
                kind="ask",
                tree=tree_of(evt),
                scope={},
                evidence=[item],
                source_key=item.key,
                expires=store.expiry(ASK_TTL),
                author=f"ask@{evt.session_id}/{evt.agent_id or 'main'}",
                created=store.now(),
            )
        )
    return None


def verbs(call: Call) -> list[str | None]:
    return [word.value for word in GRANT_CLI.bind(call).words.get("verbs", ())]


def minting(call: Call) -> bool:
    return len(said := verbs(call)) == 2 and said[0] == "grant" and said[1] in MINTING


def mint_calls(evt: BaseHookEvent) -> list[Call]:
    return [call for call in evt.cmd.calls("capt-hook") if minting(call)]


def refusal(evt: BaseHookEvent, call: Call) -> str | None:
    bound = GRANT_CLI.bind(call).values
    kind = str((bound.get("kind") or [""])[-1])
    if (declared := DECLARED.get(kind)) is None:
        return f"no hook declares grant kind `{kind}`, so nothing would judge or spend it"
    if verbs(call)[1] == "import":
        return None
    quote = str((bound.get("quote") or [""])[-1])
    owners = [*OwnerWords().collect(evt, Proposal({})), *Asked().collect(evt, Proposal({}))]
    if verbatim(quote, owners) is None:
        return "its --quote is not the owner's own words, verbatim, in this session tree"
    if declared.judge is None:
        return None
    scope = {key: value for key, _, value in (str(pair).partition("=") for pair in bound.get("scope") or ())}
    standing = bool(bound.get("unlimited"))
    proposal = Proposal(
        scope=scope,
        payload={"uses": "unlimited" if standing else str((bound.get("uses") or ["1"])[-1])},
        summary=f"record a {'standing' if standing else 'limited'} {kind} grant for {scope}",
    )
    verdict = declared.judge(evt, hook="grant-mint", action=proposal, evidence=owners, rulings=())
    return None if verdict.allow else verdict.reason


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), LambdaCondition(lambda evt: bool(mint_calls(evt)))],
    respect_gitignore=False,
    skip_planning_agents=False,
    tests={
        Input(command="capt-hook grant list"): Allow(),
        Input(command="capt-hook grant add --kind nobody.declares --scope k=v --quote hi"): Block(
            pattern="no hook declares grant kind `nobody.declares`"
        ),
    },
)
def guard_mints(evt: BaseHookEvent) -> HookResult | None:
    reasons = [why for call in mint_calls(evt) if (why := refusal(evt, call)) is not None]
    if not reasons:
        return None
    return evt.block(
        "A grant is minted only from the owner's own words, and this one was refused: "
        + "; ".join(reasons)
        + ". Ask the owner, then record their words verbatim with --quote."
    )
