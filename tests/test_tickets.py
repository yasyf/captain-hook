from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import spawnllm
from click.testing import CliRunner
from spawnllm import Decision, LabelAnswer

from captain_hook.conditions import UserSaid
from captain_hook.dispatch import execute_hook
from captain_hook.events import PreToolUseEvent
from captain_hook.tickets import (
    Allow,
    Answer,
    Context,
    Decide,
    Deny,
    Evidence,
    Holds,
    Names,
    Proposal,
    Reach,
    Rule,
    Ticket,
    Tickets,
    Touch,
    Touched,
    Undecided,
    UserAnswered,
    UserRuled,
    found,
    store,
)
from captain_hook.tickets.cli import ticket as ticket_cli
from captain_hook.tickets.decide import Verdict
from captain_hook.types import Action, And, Event, HookResult, HookSpec, Not, Or, RegisteredHook
from tests.helpers import make_ctx

TREE = "root-session"
PLACE = {"channel": "C1", "thread": "1.2"}
ELSEWHERE = {"channel": "C1", "thread": "9.9"}
ASK = "Ask the user for permission for exactly this action."


@pytest.fixture(autouse=True)
def jev_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spawnllm, "decide_sync", MagicMock(side_effect=TimeoutError("no jev in this test")))


def context(text: str = "hi", *, place: dict[str, str] | None = None, call: str = "toolu_1", **sources: Any) -> Context:
    return Context.fake(
        "test.write", place or PLACE, payload={"text": text}, summary=f"reply {text}", tree=TREE, call=call, **sources
    )


def declared(*rules: Rule, reach: Reach = Reach.PLACE, **fields: Any) -> Tickets:
    return Tickets("test.write", ("channel", "thread"), reach, rules=rules, deny=ASK, **fields)


def minted(reach: Reach = Reach.PLACE, *, place: dict[str, str] | None = None, **fields: Any) -> Ticket:
    return store.mint(
        Ticket(
            id=store.new_id(),
            kind="test.write",
            tree=TREE,
            place=place or PLACE,
            reach=reach,
            rule="seed",
            author="test",
            created=store.now() - timedelta(minutes=1),
            **fields,
        )
    )


def answer(question: str, options: dict[str, str], chosen: str = "", typed: str = "", ident: str = "ask:q#0") -> Answer:
    picked = (chosen,) if chosen else ()
    return Answer(
        id=ident,
        source="ask",
        quote=chosen or typed,
        detail=f"question: {question}\n" + "\n".join(f"option {label}: {text}" for label, text in options.items()),
        question=question,
        options=options,
        chosen=picked,
        typed=typed,
    )


def ruling(body: str, ident: str = "ccn:0207568") -> Evidence:
    return Evidence(id=ident, source="ccn", quote=body, said_at=store.now() - timedelta(days=1))


class TestReach:
    def test_a_once_ticket_covers_the_same_action_again_and_nothing_else(self) -> None:
        tickets = declared(Rule("said", UserSaid("post it")), reach=Reach.ONCE)
        first = tickets.decide(context("hi", said=["post it"]))
        assert isinstance(first, Allow) and first.minted and first.ticket.reach is Reach.ONCE
        again = tickets.decide(context("hi", call="toolu_2"))
        assert isinstance(again, Allow) and not again.minted and again.ticket.id == first.ticket.id
        assert isinstance(tickets.decide(context("other words", call="toolu_3")), Deny)

    def test_a_place_ticket_covers_every_later_action_at_its_place(self) -> None:
        tickets = declared(Rule("said", UserSaid("post it")))
        first = tickets.decide(context("hi", said=["post it"]))
        assert isinstance(first, Allow) and first.minted
        later = tickets.decide(context("another text", call="toolu_2"))
        assert isinstance(later, Allow) and not later.minted and later.ticket.id == first.ticket.id
        assert isinstance(tickets.decide(context("hi", place=ELSEWHERE, call="toolu_3")), Deny)

    def test_a_revoked_ticket_covers_nothing(self) -> None:
        ticket = minted()
        assert isinstance(declared().decide(context()), Allow)
        store.revoke(ticket.id)
        assert isinstance(declared().decide(context(call="toolu_2")), Deny)

    def test_an_expired_ticket_covers_nothing(self) -> None:
        minted(expires=store.now() - timedelta(seconds=1))
        assert isinstance(declared().decide(context()), Deny)

    def test_a_ticket_never_crosses_session_trees(self) -> None:
        minted()
        assert isinstance(declared().decide(context()), Allow)
        lane = Context.fake("test.write", PLACE, tree="another-root")
        assert isinstance(declared().decide(lane), Deny)

    def test_a_minted_ticket_carries_its_rule_evidence_and_lifetime(self) -> None:
        tickets = declared(Rule("said", UserSaid("post it")), ttl=timedelta(hours=1))
        allow = tickets.decide(context(said=["please post it now"]))
        assert isinstance(allow, Allow)
        ticket = store.load(allow.ticket.id)
        assert (ticket.rule, ticket.place, ticket.tree) == ("said", PLACE, TREE)
        assert [item.quote for item in ticket.evidence] == ["please post it now"]
        assert ticket.expires is not None and ticket.expires - ticket.created <= timedelta(hours=1)
        assert ticket.fingerprint is None


class TestRules:
    def test_the_first_rule_that_holds_mints(self) -> None:
        tickets = declared(Rule("ruled", UserRuled("channel")), Rule("said", UserSaid("post it")))
        allow = tickets.decide(context(said=["post it"], rulings=[ruling("Post freely in C1.")]))
        assert isinstance(allow, Allow) and allow.ticket.rule == "ruled"

    def test_no_rule_holding_denies_and_names_each(self) -> None:
        tickets = declared(Rule("ruled", UserRuled("channel")), Rule("said", UserSaid("post it")))
        deny = tickets.decide(context(said=["hold off"]))
        assert deny == Deny(ASK, "ruled: nothing found; said: nothing found")

    def test_a_rule_the_judge_cannot_settle_passes_to_the_next(self, tmp_path: Path) -> None:
        tickets = declared(Rule("judged", Decide("permit", llm=False)), Rule("said", UserSaid("post it")))
        allow = tickets.decide(context(said=["post it"]))
        assert isinstance(allow, Allow) and allow.ticket.rule == "said"

    def test_a_declaration_with_no_rules_only_finds(self) -> None:
        assert isinstance(declared().decide(context()), Deny)
        minted()
        assert isinstance(declared().decide(context()), Allow)

    def test_places_are_canonical(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="carry exactly"):
            declared().context(event(tmp_path), Proposal(place={"channel": "C1"}))


class TestConditions:
    def test_user_said_matches_a_regex_a_matcher_or_everything(self) -> None:
        ctx = context(said=["please post in 1.2 of C1", "unrelated"])
        assert [item.quote for item in found(UserSaid("post"), ctx) or []] == ["please post in 1.2 of C1"]
        assert [item.quote for item in found(UserSaid(Names()), ctx) or []] == ["please post in 1.2 of C1"]
        assert len(found(UserSaid(), ctx) or []) == 2
        assert found(UserSaid("never said"), ctx) is None

    def test_names_reads_only_the_keys_it_is_given(self) -> None:
        ctx = context(said=["reply in C1"])
        assert found(UserSaid(Names("channel")), ctx)
        assert found(UserSaid(Names("thread")), ctx) is None
        assert found(UserSaid(Names()), ctx) is None

    def test_holds_reads_the_payload_verbatim(self) -> None:
        ctx = context("the deploy finished", said=['post "the deploy finished" there', "archives"])
        assert [item.quote for item in found(UserSaid(Holds("text")), ctx) or []] == [
            'post "the deploy finished" there'
        ]

    def test_user_answered_counts_a_pick_other_than_the_last_option(self) -> None:
        yes = answer("Post in C1, thread 1.2?", {"Yes": "", "No": ""}, chosen="Yes")
        no = answer("Post in C1, thread 1.2?", {"Yes": "", "No": ""}, chosen="No", ident="ask:q#1")
        ctx = context(answers=[yes, no])
        assert [item.id for item in found(UserAnswered(Names()), ctx) or []] == ["ask:q#0"]
        assert [item.id for item in found(UserAnswered(Names(), approved=False), ctx) or []] == ["ask:q#0", "ask:q#1"]

    def test_user_answered_reads_typed_words_that_hold_nothing_back(self) -> None:
        go = answer("Post in C1, thread 1.2?", {"Yes": "", "No": ""}, typed="go ahead, and mention Tevon")
        wait = answer("Post in C1, thread 1.2?", {"Yes": "", "No": ""}, typed="not yet", ident="ask:q#1")
        assert [item.id for item in found(UserAnswered(), context(answers=[go, wait])) or []] == ["ask:q#0"]

    def test_user_answered_reads_the_question_not_the_place_of_another(self) -> None:
        other = answer("Post in C1, thread 9.9?", {"Yes": "", "No": ""}, chosen="Yes")
        assert found(UserAnswered(Names()), context(answers=[other])) is None

    def test_user_ruled_reads_rulings_naming_the_place_value_or_by_id(self) -> None:
        ctx = context(rulings=[ruling("Reply in C1 whenever asked."), ruling("Never post in C2.", "ccn:c9b27c1")])
        assert [item.id for item in found(UserRuled("channel"), ctx) or []] == ["ccn:0207568"]
        assert [item.id for item in found(UserRuled(ids=("c9b27c1",)), ctx) or []] == ["ccn:c9b27c1"]
        assert found(UserRuled("thread"), ctx) is None

    def test_touched_reads_the_trees_earlier_calls_naming_the_place(self) -> None:
        reply = Touch("slack_reply", {"channel_id": "C1", "thread_ts": "1.2", "text": "x"}, '{"ts": "1.3"}')
        other = Touch("slack_reply", {"channel_id": "C1", "thread_ts": "9.9", "text": "x"}, '{"ts": "9.10"}')
        ctx = context(touches=[reply, other])
        assert [item.id for item in found(Touched(), ctx) or []] == ["touch:0"]
        assert found(Touched(), context(touches=[other])) is None

    def test_combinators_gather_evidence_from_every_part(self) -> None:
        ctx = context(said=["post it"], rulings=[ruling("Reply in C1.")])
        both = found(And(UserSaid("post"), UserRuled("channel")), ctx)
        assert [item.id for item in both or []] == [ctx.said[0].id, "ccn:0207568"]
        assert found(And(UserSaid("post"), UserSaid("never")), ctx) is None
        assert found(Or(UserSaid("never"), UserRuled("channel")), ctx) == [ctx.rulings("C1")[0]]
        assert found(Not(UserSaid("never")), ctx) == []
        assert found(Not(UserSaid("post")), ctx) is None
        assert found(And(Not(UserSaid("never")), UserSaid("post")), ctx) == [ctx.said[0]]


class TestStore:
    def test_a_downstream_spender_records_one_use_per_call(self) -> None:
        ticket = minted()
        argv = ["spend", ticket.id, "--place", "channel=C1", "--place", "thread=1.2", "--tree", TREE, "--session", TREE]
        argv += ["--call", "post-1", "--fingerprint", "f1", "--summary", "reply"]
        paid = CliRunner().invoke(ticket_cli, argv)
        assert paid.exit_code == 0 and f'"id": "{ticket.id}"' in paid.output
        CliRunner().invoke(ticket_cli, argv)
        assert [(use.call, use.state) for use in store.uses(ticket.id)] == [("post-1", "committed")]
        released = CliRunner().invoke(ticket_cli, ["release", ticket.id, "--call", "post-1"])
        assert released.exit_code == 0 and '"released": 1' in released.output
        assert [use.state for use in store.uses(ticket.id)] == ["released"]

    def test_a_downstream_spend_refuses_another_place_tree_or_action(self) -> None:
        once = minted(Reach.ONCE, fingerprint="f1")
        with pytest.raises(store.SpentError, match="covers channel=C1, thread=1.2 in session tree"):
            store.spend(
                once.id,
                trees=(TREE,),
                place=ELSEWHERE,
                fingerprint="f1",
                session=TREE,
                agent="main",
                call="c",
                summary="x",
            )
        with pytest.raises(store.SpentError, match="does not include this action"):
            store.spend(
                once.id,
                trees=("other",),
                place=PLACE,
                fingerprint="f1",
                session=TREE,
                agent="main",
                call="c",
                summary="x",
            )
        with pytest.raises(store.SpentError, match="does not include this action"):
            store.spend(
                once.id, trees=(TREE,), place=PLACE, fingerprint="f2", session=TREE, agent="main", call="c", summary="x"
            )
        assert store.spend(
            once.id, trees=(TREE,), place=PLACE, fingerprint="f1", session=TREE, agent="main", call="c", summary="x"
        )

    def test_spending_an_unknown_or_revoked_ticket_says_so(self) -> None:
        with pytest.raises(store.SpentError, match="No ticket nope exists"):
            store.spend(
                "nope", trees=(TREE,), place=PLACE, fingerprint="f", session=TREE, agent="main", call="c", summary="x"
            )
        ticket = store.revoke(minted().id)
        with pytest.raises(store.SpentError, match=f"Ticket {ticket.id} was revoked at"):
            store.spend(
                ticket.id,
                trees=(TREE,),
                place=PLACE,
                fingerprint="f",
                session=TREE,
                agent="main",
                call="c",
                summary="x",
            )

    def test_the_cli_lists_shows_and_revokes(self) -> None:
        ticket = minted()
        listed = CliRunner().invoke(ticket_cli, ["list"])
        assert listed.exit_code == 0 and f"{ticket.id}  test.write" in listed.output and "live" in listed.output
        shown = CliRunner().invoke(ticket_cli, ["show", ticket.id])
        assert shown.exit_code == 0 and '"rule": "seed"' in shown.output
        revoked = CliRunner().invoke(ticket_cli, ["revoke", ticket.id])
        assert revoked.exit_code == 0 and "was revoked at" in revoked.output
        assert CliRunner().invoke(ticket_cli, ["list"]).output == ""
        assert ticket.id in CliRunner().invoke(ticket_cli, ["list", "--all"]).output


def event(
    tmp_path: Path, text: str = "hi", *, session: str = TREE, call: str = "toolu_1", verdict: Verdict | None = None
) -> PreToolUseEvent:
    ctx = make_ctx(tmp_path)
    if verdict is not None:
        ctx.call_llm = MagicMock(return_value=verdict)  # type: ignore[method-assign]
    raw = {
        "session_id": session,
        "tool_name": "slack_reply",
        "tool_use_id": call,
        "tool_input": {"text": text, **PLACE},
    }
    return PreToolUseEvent(_raw=raw, ctx=ctx)


def proposal(evt: PreToolUseEvent) -> Proposal:
    raw = cast(dict[str, Any], evt.input.raw)
    return Proposal(
        place={"channel": raw["channel"], "thread": raw["thread"]}, payload={"text": raw["text"]}, summary="reply"
    )


def jev(monkeypatch: pytest.MonkeyPatch, choice: str, *, sure: float = 0.95) -> None:
    def decide(state: str, questions: dict[str, Any], **kwargs: Any) -> Decision:
        options = list(questions["permits"].options)
        probabilities = {option: sure if option == choice else (1 - sure) / (len(options) - 1) for option in options}
        return Decision({"permits": LabelAnswer(choice, probabilities, 0.9)}, "jev-1.13.0", 120, 95.0)

    monkeypatch.setattr(spawnllm, "decide_sync", decide)


class TestDecide:
    def test_jev_sure_of_one_item_allows_on_it_without_the_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt = event(tmp_path)
        ctx = Context.fake("test.write", PLACE, said=["post it", "hold on"], tree=TREE)
        object.__setattr__(ctx, "evt", evt)
        jev(monkeypatch, ctx.said[0].id)
        assert found(Decide("permit"), ctx) == [ctx.said[0]]
        assert not cast(MagicMock, evt.ctx.call_llm).called

    def test_the_llm_allow_cites_the_words_it_relied_on(self, tmp_path: Path) -> None:
        evt = event(tmp_path, verdict=Verdict(allow=True, reason="asked for it", relied_on=["1a2b3c4d5e6f"]))
        ctx = Context.fake(
            "test.write", PLACE, said=[Evidence(id="words:1a2b3c4d5e6f", source="prompt", quote="post it")], tree=TREE
        )
        object.__setattr__(ctx, "evt", evt)
        assert [item.id for item in found(Decide("permit"), ctx) or []] == ["words:1a2b3c4d5e6f"]

    def test_an_llm_refusal_or_an_uncited_allow_finds_nothing(self, tmp_path: Path) -> None:
        refused = Verdict(allow=False, reason="no", refusal="The user never asked.")
        for verdict in (refused, Verdict(allow=True, reason="sure")):
            ctx = Context.fake("test.write", PLACE, said=["post it"], tree=TREE)
            object.__setattr__(ctx, "evt", event(tmp_path, verdict=verdict))
            assert found(Decide("permit"), ctx) is None

    def test_a_judge_with_nothing_to_read_asks_nothing(self, tmp_path: Path) -> None:
        ctx = Context.fake("test.write", PLACE, tree=TREE)
        evt = event(tmp_path, verdict=Verdict(allow=True, reason="sure", relied_on=["x"]))
        object.__setattr__(ctx, "evt", evt)
        assert found(Decide("permit"), ctx) is None
        assert not cast(MagicMock, evt.ctx.call_llm).called

    def test_a_judge_that_gives_no_verdict_is_undecided(self, tmp_path: Path) -> None:
        evt = event(tmp_path)
        evt.ctx.call_llm = MagicMock(side_effect=TimeoutError("slow"))  # type: ignore[method-assign]
        ctx = Context.fake("test.write", PLACE, said=["post it"], tree=TREE)
        object.__setattr__(ctx, "evt", evt)
        with pytest.raises(Undecided, match="TimeoutError: slow"):
            found(Decide("permit"), ctx)

    def test_the_judge_reads_only_what_over_names(self, tmp_path: Path) -> None:
        yes = answer("Post in C1, thread 1.2?", {"Yes": "", "No": ""}, chosen="Yes")
        ctx = Context.fake("test.write", PLACE, said=["unrelated"], answers=[yes], tree=TREE)
        evt = event(tmp_path, verdict=Verdict(allow=True, reason="sure", relied_on=["ask:q#0"]))
        object.__setattr__(ctx, "evt", evt)
        assert found(Decide("permit", over=(UserAnswered(),)), ctx) == [yes]
        assert "unrelated" not in str(cast(MagicMock, evt.ctx.call_llm).call_args)


def entry(tickets: Tickets) -> RegisteredHook:
    return RegisteredHook(
        spec=HookSpec(events=Event.PreToolUse, message="Needs the user's permission.", block=True, tickets=tickets),
        name="needs_ticket",
    )


class TestLift:
    def test_a_hook_with_tickets_lifts_its_block_for_a_covering_ticket(self, tmp_path: Path) -> None:
        ticket = minted()
        result = execute_hook(entry(declared(action=proposal)), event(tmp_path))
        assert result == HookResult(
            action=Action.warn, message=f"needs_ticket: allowed by ticket `{ticket.id}` (seed).", approve=False
        )

    def test_a_hook_with_tickets_keeps_its_block_and_tells_the_user_why(self, tmp_path: Path) -> None:
        tickets = declared(Rule("said", UserSaid("post it")), action=proposal)
        result = execute_hook(entry(tickets), event(tmp_path))
        assert result == HookResult(
            action=Action.block, message=ASK, system_message="needs_ticket: said: nothing found"
        )

    def test_a_failing_check_keeps_the_block(self, tmp_path: Path) -> None:
        broken = declared(action=lambda evt: Proposal(place={}))
        result = execute_hook(entry(broken), event(tmp_path))
        assert result is not None and result.action is Action.block and result.message == ASK
        assert "The ticket check failed (ValueError" in (result.system_message or "")

    def test_a_lane_reads_its_root_sessions_tickets(self, tmp_path: Path) -> None:
        minted()
        lane = event(tmp_path, session="lane-session")
        lane.ctx.root_path = tmp_path / f"{TREE}.jsonl"
        assert isinstance(declared(action=proposal).check(lane), Allow)

    def test_hook_rejects_tickets_without_a_block(self) -> None:
        from captain_hook import hook

        with pytest.raises(ValueError, match="tickets needs block=True"):
            hook(Event.PreToolUse, "x", tickets=declared(action=proposal))
        with pytest.raises(ValueError, match="needs a declaration with an action"):
            hook(Event.PreToolUse, "x", block=True, tickets=declared())
