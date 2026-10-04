from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from captain_hook.events import PreToolUseEvent
from captain_hook.grants import (
    Allowed,
    ContentMatches,
    Denied,
    Evidence,
    Grants,
    GrantVerdict,
    Judge,
    Never,
    Proposal,
    store,
)
from captain_hook.grants.records import brief
from captain_hook.hook_lint import copy_violations
from tests.helpers import make_ctx

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

TREE = "root-session"
ALERTS = "C0ALERTS"
STATUS = "C0STATUS"
ACCOUNTS = ("C0ACCTA", "C0ACCTB", "C0ACCTC")
CLASS_RULING = Evidence(
    id="ccn:543e865",
    source="ccn-answer",
    quote="Yes: reply in any alert thread",
    said_at=store.now() - timedelta(days=2),
    detail="ruling 543e865: reply in alert threads without asking?",
    key="ccn:543e865@2026-10-01T22:26:45+00:00",
    live=True,
)
ACCOUNT_WORDS = Evidence(
    id="words:acct",
    source="words",
    quote="do proper comms proactively in the account channels",
    said_at=store.now() - timedelta(minutes=30),
    key="words:acct",
)
POST_ANSWER = Evidence(
    id="ask:toolu_post#0",
    source="ask",
    quote="Post it in the thread",
    said_at=store.now() - timedelta(minutes=20),
    detail="question: Reply in the feedback thread with the cause and rollback times?",
    key="ask:toolu_post#0",
)


@dataclass(frozen=True, slots=True)
class Fixed:
    items: tuple[Evidence, ...]

    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        return list(self.items)


def write(evt: BaseHookEvent) -> Proposal:
    raw = evt._raw["tool_input"]
    thread = raw.get("thread_ts", "")
    verb = "edit" if raw.get("ts") else "post"
    return Proposal(
        scope={"channel": raw["channel_id"], "thread": thread},
        payload={"verb": verb, "text": raw["text"], "broadcast": bool(raw.get("broadcast"))},
        summary=f"{'an edit' if verb == 'edit' else 'a reply' if thread else 'a post'} in {raw['channel_id']}",
    )


def slack(*items: Evidence) -> Grants:
    return Grants(
        "replay.slack",
        ("channel", "thread"),
        write,
        rules=(
            ContentMatches(),
            Never("no-broadcast", lambda action: bool(action.payload.get("broadcast")), "never a broadcast"),
        ),
        judge=Judge("rules"),
        evidence=(Fixed(items),),
        standing_ttl=timedelta(days=7),
        standing_rules=("no-broadcast",),
        widen=("channel", "thread"),
        would_allow="Draft the message and ask the user to approve that exact text.",
    )


def call(
    tmp_path: Path, channel: str, *, thread: str = "", ts: str = "", text: str = "update", **verdicts: Any
) -> PreToolUseEvent:
    ctx = make_ctx(tmp_path)
    if verdicts:
        found = verdicts.pop("verdicts", None) or [GrantVerdict(**verdicts)]
        ctx.call_llm = MagicMock(side_effect=found)  # type: ignore[method-assign]
    raw = {
        "session_id": TREE,
        "tool_name": "slack_reply",
        "tool_use_id": f"toolu_{channel}_{thread}_{ts}_{text}"[:60],
        "tool_input": {"channel_id": channel, "thread_ts": thread, "ts": ts, "text": text},
    }
    return PreToolUseEvent(_raw=raw, ctx=ctx)


def allowed(verdict: Allowed | Denied) -> Allowed:
    assert isinstance(verdict, Allowed), verdict
    return verdict


def refused(verdict: Allowed | Denied) -> Denied:
    assert isinstance(verdict, Denied), verdict
    assert copy_violations(verdict.message) == []
    return verdict


def test_edits_under_a_class_ruling_pass_when_the_judge_cites_its_short_id(tmp_path: Path) -> None:
    grants = slack(CLASS_RULING)
    first = grants.check(
        call(
            tmp_path,
            ALERTS,
            thread="1.1",
            ts="1.5",
            allow=True,
            reason="The ruling covers edits of the agent's own reply in an alert thread.",
            relied_on=["543e865"],
            scope={"thread": "*"},
        )
    )
    covered = allowed(first)
    assert covered.grant.scope == {"channel": ALERTS, "thread": "*"}
    again = call(
        tmp_path, ALERTS, thread="1.1", ts="1.5", text="mechanism, corrected", allow=True, reason="ok", relied_on=[]
    )
    assert allowed(grants.check(again)).grant.id == covered.grant.id


def test_a_class_ruling_minted_for_one_alert_thread_covers_the_next_thread(tmp_path: Path) -> None:
    grants = slack(CLASS_RULING)
    first = allowed(
        grants.check(
            call(
                tmp_path,
                ALERTS,
                thread="1.1",
                allow=True,
                reason="alert reply",
                relied_on=["[ccn:543e865]"],
                scope={"thread": "*"},
            )
        )
    )
    second = grants.check(call(tmp_path, ALERTS, thread="2.2", text="fix live", allow=True, reason="ok"))
    assert allowed(second).grant.id == first.grant.id
    assert store.grants("replay.slack", TREE) == [first.grant]


def test_a_per_destination_ruling_grant_never_blocks_another_thread(tmp_path: Path) -> None:
    grants = slack(CLASS_RULING)
    allowed(grants.check(call(tmp_path, ALERTS, thread="1.1", allow=True, reason="ok", relied_on=["ccn:543e865"])))
    other = grants.check(call(tmp_path, ALERTS, thread="2.2", allow=True, reason="ok", relied_on=["ccn:543e865"]))
    assert allowed(other).grant.scope == {"channel": ALERTS, "thread": "2.2"}


def test_standing_account_channel_words_cover_each_named_account_channel(tmp_path: Path) -> None:
    grants = slack(ACCOUNT_WORDS)
    covered = allowed(
        grants.check(
            call(
                tmp_path,
                ACCOUNTS[0],
                allow=True,
                reason="The owner asked for proactive comms in the account channels.",
                relied_on=["words:acct"],
                standing=ACCOUNT_WORDS.quote,
                scope={"channel": list(ACCOUNTS)},
            )
        )
    )
    assert covered.grant.scope == {"channel": sorted(ACCOUNTS), "thread": ""}
    for channel in ACCOUNTS[1:]:
        assert allowed(grants.check(call(tmp_path, channel, allow=True, reason="ok"))).grant.id == covered.grant.id
    outside = refused(grants.check(call(tmp_path, STATUS, allow=False, reason="no", refusal="Not an account channel.")))
    assert outside.reason == "Not an account channel."


def test_standing_words_without_a_named_set_mint_one_grant_per_channel(tmp_path: Path) -> None:
    grants = slack(ACCOUNT_WORDS)
    for channel in ACCOUNTS:
        verdict = grants.check(
            call(tmp_path, channel, allow=True, reason="ok", relied_on=["words:acct"], standing=ACCOUNT_WORDS.quote)
        )
        assert allowed(verdict).grant.scope == {"channel": channel, "thread": ""}
    assert len(store.grants("replay.slack", TREE)) == len(ACCOUNTS)


def test_a_chosen_option_covers_its_own_thread_after_writes_elsewhere(tmp_path: Path) -> None:
    grants = slack(ACCOUNT_WORDS, POST_ANSWER)
    allowed(
        grants.check(
            call(tmp_path, ACCOUNTS[0], allow=True, reason="ok", relied_on=["words:acct"], standing=ACCOUNT_WORDS.quote)
        )
    )
    reply = grants.check(
        call(
            tmp_path,
            "C0FEEDBACK",
            thread="9.9",
            allow=True,
            reason="The owner chose to post.",
            relied_on=["ask:toolu_post#0"],
        )
    )
    assert allowed(reply).grant.uses == 1


def test_an_uncited_allow_names_the_missing_citation_without_ids(tmp_path: Path) -> None:
    grants = slack(ACCOUNT_WORDS)
    denied = refused(grants.check(call(tmp_path, ACCOUNTS[0], allow=True, reason="ok", relied_on=["words:else"])))
    assert (
        denied.reason == "The judge allowed this without citing any of the owner's words, so the allow does not count."
    )
    assert "words:else" in denied.explained


def test_a_draft_beyond_the_approved_gist_still_refuses_with_its_reason_in_the_block(tmp_path: Path) -> None:
    grants = slack(POST_ANSWER)
    denied = refused(
        grants.check(
            call(
                tmp_path,
                "C0FEEDBACK",
                thread="9.9",
                allow=False,
                reason='The owner chose "Post it in the thread" for the cause and rollback times [ask:toolu_post#0].',
                refusal="The owner approved the cause and rollback times, but this draft adds recovery counts.",
            )
        )
    )
    assert denied.message == (
        "The owner approved the cause and rollback times, but this draft adds recovery counts."
        " Draft the message and ask the user to approve that exact text."
    )
    assert "[ask:toolu_post#0]" in denied.explained


def test_a_judge_reason_with_quotes_and_ids_reaches_the_agent_scrubbed(tmp_path: Path) -> None:
    grants = slack(CLASS_RULING)
    denied = refused(
        grants.check(
            call(
                tmp_path,
                STATUS,
                allow=False,
                reason='Ruling [ccn:543e865] says "reply in any alert thread", and this is a top-level status post.',
            )
        )
    )
    assert denied.reason.startswith("Ruling")
    assert copy_violations(denied.message) == []


def test_a_spent_one_shot_names_what_it_was_spent_on_without_ids(tmp_path: Path) -> None:
    grants = slack(POST_ANSWER)
    approved = {"allow": True, "reason": "ok", "relied_on": ["ask:toolu_post#0"]}
    allowed(grants.check(call(tmp_path, "C0FEEDBACK", thread="9.9", text="sent", **approved)))
    again = refused(grants.check(call(tmp_path, "C0FEEDBACK", thread="9.9", text="sent again", **approved)))
    assert again.reason in (
        "The approval that covered this was already used on a reply in C0FEEDBACK.",
        "The owner's approval already covers channel=C0FEEDBACK, thread=9.9, not this action.",
    )


def test_a_stored_grant_and_a_fresh_approval_judge_twice_in_one_event(tmp_path: Path) -> None:
    grants = slack(CLASS_RULING, POST_ANSWER)
    allowed(
        grants.check(
            call(
                tmp_path,
                ALERTS,
                thread="1.1",
                allow=True,
                reason="ok",
                relied_on=["ccn:543e865"],
                scope={"thread": "*"},
            )
        )
    )
    both = call(
        tmp_path,
        ALERTS,
        thread="3.3",
        text="a different kind of post",
        verdicts=[
            GrantVerdict(reason="Not an alert update.", allow=False, refusal="The class covers alert updates only."),
            GrantVerdict(reason="The owner chose to post.", allow=True, relied_on=["ask:toolu_post#0"]),
        ],
    )
    assert allowed(grants.check(both)).grant.evidence[0].id == "ask:toolu_post#0"
    assert both.ctx.call_llm.call_count == 2


@pytest.mark.parametrize(
    ("scope", "why"),
    [
        pytest.param({"thread": "*"}, "a top-level post", id="thread-class-never-covers-top-level"),
        pytest.param({"tool": "x"}, "an undeclared key", id="undeclared-key"),
    ],
)
def test_the_judge_never_widens_past_the_action(tmp_path: Path, scope: dict[str, Any], why: str) -> None:
    grants = slack(CLASS_RULING)
    denied = refused(
        grants.check(call(tmp_path, ALERTS, allow=True, reason=why, relied_on=["ccn:543e865"], scope=scope))
    )
    assert denied.reason == "The owner's words do not reach as far as the judge read them."


def test_a_declaration_without_widen_keeps_the_action_scope(tmp_path: Path) -> None:
    narrow = Grants(
        "replay.narrow",
        ("channel", "thread"),
        write,
        judge=Judge("rules"),
        evidence=(Fixed((CLASS_RULING,)),),
    )
    denied = refused(
        narrow.check(
            call(
                tmp_path,
                ALERTS,
                thread="1.1",
                allow=True,
                reason="ok",
                relied_on=["ccn:543e865"],
                scope={"thread": "*"},
            )
        )
    )
    assert "do not reach" in denied.reason


def test_a_requested_channel_set_grant_needs_verbatim_words_and_the_judge(tmp_path: Path) -> None:
    grants = slack(ACCOUNT_WORDS)
    paraphrase = refused(
        grants.request(
            call(tmp_path, ACCOUNTS[0], allow=True, reason="ok"),
            scope={"channel": list(ACCOUNTS), "thread": ""},
            quote="post in the account channels",
        )
    )
    assert "not verbatim" in paraphrase.reason
    general = refused(
        grants.request(
            call(
                tmp_path,
                ACCOUNTS[0],
                allow=False,
                reason="General words name no channel.",
                refusal="These words name no channel, so they grant no standing posts.",
            ),
            scope={"channel": STATUS, "thread": ""},
            quote="proactively",
        )
    )
    assert general.reason == "These words name no channel, so they grant no standing posts."
    recorded = allowed(
        grants.request(
            call(tmp_path, ACCOUNTS[0], allow=True, reason="The words name the account channels."),
            scope={"channel": list(ACCOUNTS), "thread": ""},
            quote=ACCOUNT_WORDS.quote,
        )
    )
    assert recorded.remaining is None
    assert recorded.grant.scope == {"channel": sorted(ACCOUNTS), "thread": ""}
    posted = grants.check(call(tmp_path, ACCOUNTS[2], allow=True, reason="ok"))
    assert allowed(posted).grant.id == recorded.grant.id


def test_a_requested_thread_grant_rests_on_the_ruling_that_quotes_the_owner(tmp_path: Path) -> None:
    grants = slack(CLASS_RULING)
    recorded = allowed(
        grants.request(
            call(tmp_path, ALERTS, thread="1.1", allow=True, reason="The ruling covers replies in alert threads."),
            scope={"channel": ALERTS, "thread": "1.1"},
            quote=CLASS_RULING.quote,
        )
    )
    assert recorded.remaining is None and recorded.grant.scope == {"channel": ALERTS, "thread": "1.1"}
    (basis,) = recorded.grant.evidence
    assert (basis.id, basis.key, basis.live) == (CLASS_RULING.id, CLASS_RULING.key, True)
    posted = grants.check(call(tmp_path, ALERTS, thread="1.1", text="mechanism", allow=True, reason="ok"))
    assert allowed(posted).grant.id == recorded.grant.id
    paraphrase = refused(
        grants.request(
            call(tmp_path, ALERTS, thread="2.2", allow=True, reason="ok"),
            scope={"channel": ALERTS, "thread": "2.2"},
            quote="reply in alert threads",
        )
    )
    assert "not verbatim" in paraphrase.reason


def test_a_counted_approval_spends_across_the_places_it_names(tmp_path: Path) -> None:
    grants = slack(POST_ANSWER)
    threads = ["1.1", "2.2"]
    first = allowed(
        grants.check(
            call(
                tmp_path,
                ALERTS,
                thread=threads[0],
                allow=True,
                reason="two replies",
                relied_on=["ask:toolu_post#0"],
                uses=2,
                scope={"thread": threads},
            )
        )
    )
    assert first.remaining == 1
    second = grants.check(call(tmp_path, ALERTS, thread=threads[1], text="second", allow=True, reason="ok"))
    assert allowed(second).remaining == 0
    third = refused(
        grants.check(
            call(
                tmp_path,
                ALERTS,
                thread=threads[1],
                text="third",
                allow=True,
                reason="ok",
                relied_on=["ask:toolu_post#0"],
            )
        )
    )
    assert third.reason.startswith("The approval that covered this was already used")


@pytest.mark.parametrize(
    ("judged", "agent"),
    [
        pytest.param(
            "Ruling 543e865 covers replies, not edits [ccn:543e865]. It says more.",
            "Decision covers replies, not edits.",
            id="short-id-and-ruling",
        ),
        pytest.param(
            "the reply states 21:50 UTC; Slack posts give Pacific times with no label",
            "the reply states a stated time; Slack posts give Pacific times with no label.",
            id="clock-time",
        ),
    ],
)
def test_brief_reasons_meet_the_copy_bar(judged: str, agent: str) -> None:
    assert brief(judged) == agent
    assert copy_violations(f"{brief(judged)} Ask the owner.") == []
