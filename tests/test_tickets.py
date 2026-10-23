from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import spawnllm
from click.testing import CliRunner
from spawnllm import BackendCallError, Decision, LabelAnswer

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
DRAFT = "_(Yasyf's Claude)_ Hi Michael, the box has been live since Sep 23."
BOARD = {
    "doc": {
        "blocks": [
            {
                "id": "card-gap",
                "type": "card",
                "title": "5. Reply to Michael in #reco-gap",
                "summary": "The bot is not in that channel, so this posts through your account.",
                "children": [
                    {"id": "gap-draft", "type": "markdown", "md": DRAFT},
                    {"id": "gap-approval", "type": "approval", "prompt": "Post this reply in his thread?"},
                ],
            },
            {
                "id": "card-perm",
                "type": "card",
                "title": "3. Grant the app actions: read",
                "children": [{"id": "perm-approval", "type": "approval", "prompt": "Will you grant it?"}],
            },
        ]
    },
    "interactions": {
        "decisions": {"gap-approval": {"verdict": "approved"}, "perm-approval": {"verdict": "rejected"}},
        "feedback": {"perm-approval": [{"id": "f1", "text": "you'll have to DM josh L on slack"}]},
    },
}


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

    def test_an_unchecked_ticket_covers_this_action_once_and_records_the_cause(self) -> None:
        tickets = declared(Rule("said", UserSaid("post it")))
        ctx = context("hi")
        ticket = tickets.unchecked(ctx, "TimeoutExpired: cc-slack dm-peer")
        assert (ticket.reach, ticket.rule, ticket.fingerprint) == (Reach.ONCE, "unchecked", ctx.action.fingerprint)
        assert [(item.source, item.quote) for item in ticket.evidence] == [("hook", "TimeoutExpired: cc-slack dm-peer")]
        assert ticket.expires is not None and ticket.expires - ticket.created == timedelta(minutes=10)
        again = tickets.decide(context("hi", call="toolu_2"))
        assert isinstance(again, Allow) and not again.minted and again.ticket.id == ticket.id
        assert isinstance(tickets.decide(context("other words", call="toolu_3")), Deny)

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

    def test_words_typed_beyond_a_pick_decide_over_the_pick(self) -> None:
        options = {"Restack and merge": "", "Wait for Sikan": "", "Leave it to Sikan": ""}
        rider = answer(
            "Sikan's fix is approved. What now?",
            options,
            chosen="Leave it to Sikan",
            typed="3, DM him and ask if he needs help",
        )
        bare = answer(
            "Sikan's fix is approved. What now?", options, chosen="Leave it to Sikan", typed="3", ident="ask:q#1"
        )
        held = answer(
            "Sikan's fix is approved. What now?",
            options,
            chosen="Restack and merge",
            typed="1, but wait for CI",
            ident="ask:q#2",
        )
        assert [item.id for item in found(UserAnswered(), context(answers=[rider, bare, held])) or []] == ["ask:q#0"]

    def test_a_last_option_that_reads_as_a_go_approves(self) -> None:
        options = {"Post both as shown (Recommended)": "", "Post without the prevention sentence": ""}
        trimmed = answer(
            "Approve these two exact thread replies?", options, chosen="Post without the prevention sentence"
        )
        only = answer(
            "Reply in alert threads?",
            {"Yes: reply in any alert thread": ""},
            chosen="Yes: reply in any alert thread",
            ident="ask:q#1",
        )
        edit = answer(
            "Approve this text?", {"Approve as shown": "", "Edit first": ""}, chosen="Edit first", ident="ask:q#2"
        )
        assert [item.id for item in found(UserAnswered(), context(answers=[trimmed, only, edit])) or []] == [
            "ask:q#0",
            "ask:q#1",
        ]

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


class TestBoard:
    def test_a_decision_approves_only_when_its_verdict_is_approved(self) -> None:
        from captain_hook.tickets.conditions import approving
        from captain_hook.tickets.context import board_answers

        by_id = {item.id: item for item in board_answers(BOARD)}
        assert approving(by_id["board:decision:gap-approval"])
        assert not approving(by_id["board:decision:perm-approval"])
        assert approving(by_id["board:feedback:perm-approval:f1"])
        assert by_id["board:decision:perm-approval"].options == {"approved": "", "rejected": ""}

    def test_an_answer_carries_the_card_the_user_read(self) -> None:
        from captain_hook.tickets.context import board_answers

        approval = next(item for item in board_answers(BOARD) if item.id == "board:decision:gap-approval")
        assert approval.detail.startswith(
            "board card: 5. Reply to Michael in #reco-gap\n"
            "The bot is not in that channel, so this posts through your account.\n"
            f"{DRAFT}\n"
            "question: Post this reply in his thread?"
        )
        ctx = context(DRAFT, answers=[approval])
        assert found(UserAnswered(Holds("text")), ctx) == [approval]
        assert found(UserAnswered(Names()), ctx) is None

    def test_answers_are_read_past_the_window_and_touches_within_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from captain_hook import T
        from captain_hook.context import HookContext
        from captain_hook.session import SessionStore
        from captain_hook.testing.helpers import fixture_session
        from captain_hook.tickets import context as sources

        monkeypatch.setattr(sources, "board_state", lambda tree, cwd: {})

        question = "Post in C1, thread 1.2?"
        questions = [{"question": question, "options": [{"label": "Yes"}, {"label": "No"}]}]
        ask = T.tool("AskUserQuestion", id="toolu_ask", questions=questions)
        read = T.tool("slack_thread", id="toolu_read", channel_id="C1", thread_ts="1.2")
        transcript = fixture_session(
            [
                T.assistant(ask),
                T.user(
                    T.result(f'User has answered your questions: "{question}"="Yes".', of=ask),
                    toolUseResult={"questions": questions, "answers": {question: "Yes"}, "annotations": {}},
                ),
                T.assistant(read),
                T.user(T.result('{"messages": []}', of=read)),
                *(turn for i in range(40) for turn in (T.user(f"status {i}"), T.assistant("still going"))),
            ]
        )
        evt = PreToolUseEvent(
            _raw={"session_id": TREE, "tool_name": "slack_reply", "tool_use_id": "toolu_1", "tool_input": {}},
            ctx=HookContext(session=SessionStore(tmp_path), transcript=transcript, settings=None),
        )
        ctx = Context.of(evt, "test.write", Proposal(PLACE, {"text": "x"}), window=20)
        assert [item.id for item in ctx.answers] == ["ask:toolu_ask#0"]
        assert ctx.touches == ()
        wide = Context.of(evt, "test.write", Proposal(PLACE, {"text": "x"}), window=200)
        assert [touch.tool for touch in wide.touches] == ["slack_thread"]

    def test_a_tap_on_a_cc_slack_question_card_is_an_answer_and_no_other_channel_line_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from captain_hook import T
        from captain_hook.context import HookContext
        from captain_hook.session import SessionStore
        from captain_hook.testing.helpers import fixture_session
        from captain_hook.tickets import context as sources
        from captain_hook.tickets.conditions import approving

        monkeypatch.setattr(sources, "board_state", lambda tree, cwd: {})
        question = 'Post this in C1, thread 1.2? "the fix is live"'
        channel = {"kind": "channel", "server": sources.CARD_SERVER}

        def tap(action_ts: str, label: str, server: str = sources.CARD_SERVER, **fields: Any) -> str:
            payload = {
                "type": "slack.action",
                "action_ts": action_ts,
                "label": label,
                "waits": True,
                "question": question,
                "options": ["Yes", "No"],
            } | fields
            envelope = f'<channel source="{server}" subject_id="s1" type="slack.action">'
            return f"{envelope}\n{json.dumps(payload)}\n</channel>"

        queued = {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "prompt": tap("1.5", "Yes"),
                "commandMode": "prompt",
                "origin": channel,
            },
        }
        transcript = fixture_session(
            [
                T.user("watch the release"),
                T.assistant("asked on the card"),
                T.user(tap("1.3", "No"), isMeta=True, origin=channel),
                T.user(tap("1.4", "Explain more"), isMeta=True, origin=channel),
                T.user(tap("1.6", "Yes", options=[]), isMeta=True, origin=channel),
                T.user(tap("1.7", "Yes", server="plugin:other:other"), isMeta=True, origin=channel),
                T.user(tap("1.8", "Yes")),
                queued,
                T.user(tap("1.5", "Yes"), isMeta=True, origin=channel),
            ]
        )
        evt = PreToolUseEvent(
            _raw={"session_id": TREE, "tool_name": "slack_reply", "tool_use_id": "toolu_1", "tool_input": {}},
            ctx=HookContext(session=SessionStore(tmp_path), transcript=transcript, settings=None),
        )
        ctx = Context.of(evt, "test.write", Proposal(PLACE, {"text": "the fix is live"}))
        assert [(item.id, item.chosen) for item in ctx.answers] == [("card:1.3", ("No",)), ("card:1.5", ("Yes",))]
        assert [approving(item) for item in ctx.answers] == [False, True]
        assert found(UserAnswered(Holds("text")), ctx) == [ctx.answers[1]]

    def test_a_rejected_call_hands_the_judge_only_the_users_words(self, tmp_path: Path) -> None:
        from cc_transcript.filterspec import USER_SAID_MARKER, USER_SAID_TRAILER

        from captain_hook import T
        from captain_hook.context import HookContext
        from captain_hook.session import SessionStore
        from captain_hook.testing.helpers import fixture_session

        typed = "also need to create a thread in epd-sprint1 and update anubhav there"
        banner = "The user doesn't want to proceed with this tool use. The tool use was rejected. "
        wrapper = f"{banner}{USER_SAID_MARKER}{typed}\n\n{USER_SAID_TRAILER} may contain a correction."
        send = T.tool("slack_send", id="toolu_send", channel_id="C1", text="hi")
        transcript = fixture_session(
            [T.user("post it"), T.assistant(send), T.user(T.result(wrapper, of=send, is_error=True)), T.user(wrapper)]
        )
        evt = PreToolUseEvent(
            _raw={"session_id": TREE, "tool_name": "slack_reply", "tool_use_id": "toolu_1", "tool_input": {}},
            ctx=HookContext(session=SessionStore(tmp_path), transcript=transcript, settings=None),
        )
        ctx = Context.of(evt, "test.write", Proposal(PLACE, {"text": "x"}))
        assert [(item.source, item.quote) for item in ctx.said] == [("prompt", "post it"), ("instruction", typed)]
        assert all("rejecting" in item.detail for item in ctx.said if item.source == "instruction")
        assert not any(USER_SAID_MARKER in item.quote for item in ctx.said)

    def test_a_session_with_no_transcript_yet_started_now(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from captain_hook.context import HookContext
        from captain_hook.session import SessionStore
        from captain_hook.testing.helpers import fixture_session
        from captain_hook.tickets.context import session_started

        evt = PreToolUseEvent(
            _raw={"session_id": TREE, "tool_name": "slack_reply", "tool_use_id": "toolu_1", "tool_input": {}},
            ctx=HookContext(session=SessionStore(tmp_path), transcript=fixture_session([]), settings=None),
        )
        assert abs((session_started(evt) - datetime.now(UTC)).total_seconds()) < 5


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

    def test_a_citation_with_a_mangled_prefix_still_names_its_item(self, tmp_path: Path) -> None:
        feedback = Evidence(id="board:feedback:app-perm:4d39f078", source="board", quote="share the url with josh")
        for cite in ("words:4d39f078", "[board:feedback:app-perm:4d39f078] board said 2026-10-03 5:16 PM"):
            evt = event(tmp_path, verdict=Verdict(allow=True, reason="asked for it", relied_on=[cite]))
            ctx = Context.fake("test.write", PLACE, said=[feedback], tree=TREE)
            object.__setattr__(ctx, "evt", evt)
            assert found(Decide("permit"), ctx) == [feedback]

    def test_a_citation_that_quotes_the_users_words_names_their_item(self, tmp_path: Path) -> None:
        said = Evidence(id="words:9f8e7d6c5b4a", source="prompt", quote="post it in the alerts thread")
        for cite, named in (("user's words: post it in the alerts thread", [said]), ("x", [])):
            evt = event(tmp_path, verdict=Verdict(allow=True, reason="asked for it", relied_on=[cite]))
            ctx = Context.fake("test.write", PLACE, said=[said], tree=TREE)
            object.__setattr__(ctx, "evt", evt)
            assert (found(Decide("permit"), ctx) or []) == named

    def test_an_llm_refusal_or_an_uncited_allow_finds_nothing(self, tmp_path: Path) -> None:
        refused = Verdict(allow=False, reason="no", refusal="The user never asked.")
        for verdict in (refused, Verdict(allow=True, reason="sure")):
            ctx = Context.fake("test.write", PLACE, said=["post it"], tree=TREE)
            evt = event(tmp_path, verdict=verdict)
            object.__setattr__(ctx, "evt", evt)
            assert found(Decide("permit"), ctx) is None
            assert cast(MagicMock, evt.ctx.call_llm).call_count == (2 if verdict is refused else 1)

    def test_one_refusal_is_asked_again_and_a_second_allow_counts(self, tmp_path: Path) -> None:
        said = Evidence(id="words:1a2b3c4d5e6f", source="prompt", quote="post it")
        refused = Verdict(allow=False, reason="no", refusal="The user never asked.")
        evt = event(tmp_path)
        evt.ctx.call_llm = MagicMock(  # type: ignore[method-assign]
            side_effect=[refused, Verdict(allow=True, reason="asked for it", relied_on=[said.id])]
        )
        ctx = Context.fake("test.write", PLACE, said=[said], tree=TREE)
        object.__setattr__(ctx, "evt", evt)
        assert found(Decide("permit"), ctx) == [said]
        once = Context.fake("test.write", PLACE, said=[said], tree=TREE)
        object.__setattr__(once, "evt", event(tmp_path, verdict=refused))
        assert found(Decide("permit", second_opinion=False), once) is None
        assert cast(MagicMock, once.evt.ctx.call_llm).call_count == 1

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

    def test_a_judge_that_overflows_its_window_is_asked_again_without_the_transcript(self, tmp_path: Path) -> None:
        evt = event(tmp_path)
        overflow = BackendCallError(
            "apple generation failed (ExceededContextWindowSizeError): the prompt exceeded the model's context window"
        )
        said = Evidence(id="words:1a2b3c4d5e6f", source="prompt", quote="post it")
        evt.ctx.call_llm = MagicMock(  # type: ignore[method-assign]
            side_effect=[overflow, Verdict(allow=True, reason="asked for it", relied_on=[said.id])]
        )
        windows: list[tuple[Any, Any]] = []
        assemble = evt.ctx.assemble_prompt

        def spying(*args: Any, **kwargs: Any) -> str:
            windows.append((kwargs["transcript"], kwargs["root_transcript"]))
            return assemble(*args, **kwargs)

        evt.ctx.assemble_prompt = spying  # type: ignore[method-assign]
        ctx = Context.fake("test.write", PLACE, said=[said], tree=TREE)
        object.__setattr__(ctx, "evt", evt)
        assert found(Decide("permit", transcript=12), ctx) == [said]
        assert cast(MagicMock, evt.ctx.call_llm).call_count == 2
        assert windows == [(12, False), (False, False)]

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
