from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from captain_hook import Event
from captain_hook.dispatch import denies, execute_hook
from captain_hook.events import PreToolUseEvent
from captain_hook.grants import (
    Allowed,
    ContentMatches,
    Denied,
    Evidence,
    Grant,
    Grants,
    GrantVerdict,
    Judge,
    Never,
    OwnerWords,
    Proposal,
    Rulings,
    StandingRulings,
    reservations,
    settle,
    store,
)
from captain_hook.grants import cli as grant_cli_module
from captain_hook.grants import evidence as evidence_module
from captain_hook.grants import orca as orca_module
from captain_hook.grants.cli import grant as grant_cli
from captain_hook.hook_lint import result_violations
from captain_hook.types import Action, HookResult, HookSpec, RegisteredHook
from tests.helpers import make_ctx

TREE = "root-session"
SCOPE = {"channel": "C1", "thread": "1.2"}
STANDING = "reply in that thread without asking"


@dataclass(frozen=True, slots=True)
class Fixed:
    items: tuple[Evidence, ...]

    def collect(self, evt: Any, action: Proposal) -> list[Evidence]:
        return list(self.items)


def owner(quote: str, *, at: datetime | None = None, ident: str = "words:1") -> Evidence:
    return Evidence(id=ident, source="words", quote=quote, said_at=at or store.now(), key=ident)


def event(tmp_path: Path, text: str = "hi", *, session: str = TREE, call: str = "toolu_1", **verdict: Any) -> Any:
    ctx = make_ctx(tmp_path)
    if verdict:
        ctx.call_llm = MagicMock(return_value=GrantVerdict(**verdict))  # type: ignore[method-assign]
    raw = {
        "session_id": session,
        "tool_name": "slack_reply",
        "tool_use_id": call,
        "tool_input": {"text": text, **SCOPE},
    }
    return PreToolUseEvent(_raw=raw, ctx=ctx)


def proposal(evt: Any) -> Proposal:
    raw = evt.input.raw
    return Proposal(
        scope={"channel": raw["channel"], "thread": raw["thread"]},
        payload={"text": raw["text"]},
        summary=f"reply in {raw['channel']}/{raw['thread']}",
    )


def declared(**fields: Any) -> Grants:
    return Grants("test.write", ("channel", "thread"), proposal, **fields)


def minted(**fields: Any) -> Grant:
    return store.mint(
        Grant(
            id=store.new_id(),
            kind="test.write",
            tree=TREE,
            scope=SCOPE,
            author="test",
            created=store.now() - timedelta(minutes=1),
            **fields,
        )
    )


def test_a_one_shot_grant_covers_one_action_then_names_its_spend(tmp_path: Path) -> None:
    grant = minted()
    first = declared().check(event(tmp_path, "one"))
    assert isinstance(first, Allowed) and first.grant.id == grant.id and first.remaining == 0
    second = declared().check(event(tmp_path, "two", call="toolu_2"))
    assert isinstance(second, Denied)
    assert f"Grant {grant.id} was spent at" in second.explained
    assert "on reply in C1/1.2" in second.reason


def test_a_one_shot_retry_of_the_same_payload_spends_nothing_more(tmp_path: Path) -> None:
    minted()
    assert declared().check(event(tmp_path, "same"))
    assert declared().check(event(tmp_path, "same", call="toolu_2"))
    assert not declared().check(event(tmp_path, "other", call="toolu_3"))


def test_n_uses_then_none(tmp_path: Path) -> None:
    minted(uses=2)
    assert [bool(declared().check(event(tmp_path, f"t{n}", call=f"c{n}"))) for n in range(3)] == [True, True, False]


def test_an_unlimited_grant_stops_at_its_expiry(tmp_path: Path) -> None:
    grant = minted(uses=None, expires=store.now() + timedelta(hours=1))
    assert all(declared().check(event(tmp_path, f"t{n}", call=f"c{n}")) for n in range(5))
    store.save(grant.model_copy(update={"expires": store.now() - timedelta(seconds=1)}))
    denied = declared().check(event(tmp_path, "late", call="late"))
    assert isinstance(denied, Denied) and "expired at" in denied.explained


def test_a_revoked_grant_covers_nothing(tmp_path: Path) -> None:
    grant = minted(uses=None)
    store.revoke(grant.id)
    assert not declared().check(event(tmp_path))
    assert "revoked" in str(store.unusable(store.load(grant.id), [], store.now()))


def test_a_grant_never_crosses_session_trees(tmp_path: Path) -> None:
    minted(uses=None)
    assert declared().check(event(tmp_path, session=TREE))
    assert not declared().check(event(tmp_path, session="another-root", call="c2"))


def test_a_lane_spends_its_root_sessions_grant(tmp_path: Path) -> None:
    minted(uses=None)
    lane = event(tmp_path, session="lane-session")
    lane.ctx.root_path = tmp_path / f"{TREE}.jsonl"
    assert declared().check(lane)


def test_scopes_are_canonical(tmp_path: Path) -> None:
    partial = Grants("test.write", ("channel", "thread"), lambda evt: Proposal(scope={"channel": "C1"}))
    with pytest.raises(ValueError, match="carry exactly"):
        partial.check(event(tmp_path))


def test_an_omitted_thread_is_never_a_wildcard(tmp_path: Path) -> None:
    store.mint(
        Grant(
            id=store.new_id(),
            kind="test.write",
            tree=TREE,
            scope={"channel": "C1", "thread": ""},
            uses=None,
            author="t",
            created=store.now(),
        )
    )
    assert not declared().check(event(tmp_path))


def test_a_rule_the_grant_names_denies(tmp_path: Path) -> None:
    never_edit = Never(
        "no-long", lambda action: len(action.payload["text"]) > 5, "a standing grant never covers long text"
    )
    minted(uses=None, rules=["no-long"])
    denied = declared(rules=(never_edit,)).check(event(tmp_path, "much too long"))
    assert isinstance(denied, Denied) and "never covers long text" in denied.reason
    assert declared(rules=(never_edit,)).check(event(tmp_path, "short", call="c2"))


def test_a_rule_the_grant_does_not_name_stays_off(tmp_path: Path) -> None:
    never_edit = Never("no-long", lambda action: True, "never")
    minted(uses=None)
    assert declared(rules=(never_edit,)).check(event(tmp_path))


def test_matching_content_settles_without_the_judge(tmp_path: Path) -> None:
    minted(approved={"text": "the approved text"})
    evt = event(tmp_path, "the approved text", allow=False, reason="unused")
    assert declared(rules=(ContentMatches(),), judge=Judge("rules")).check(evt)
    evt.ctx.call_llm.assert_not_called()


def test_different_content_goes_to_the_judge_with_a_diff(tmp_path: Path) -> None:
    minted(approved={"text": "the approved text"})
    evt = event(tmp_path, "the changed text", allow=False, reason="the owner approved other words")
    denied = declared(rules=(ContentMatches(),), judge=Judge("rules")).check(evt)
    assert isinstance(denied, Denied) and "the owner approved other words" in denied.reason
    prompt = str(evt.ctx.call_llm.call_args.args[0])
    assert "- approved" in prompt and "+ changed" in prompt


def test_owner_words_after_a_grant_reach_the_judge_before_any_spend(tmp_path: Path) -> None:
    minted(approved={"text": "ok"})
    later = owner("actually hold off, let me review each reply first", at=store.now())
    evt = event(tmp_path, "ok", allow=False, reason="the owner withdrew it")
    denied = declared(rules=(ContentMatches(),), judge=Judge("rules"), evidence=(Fixed((later,)),)).check(evt)
    assert isinstance(denied, Denied) and "withdrew" in denied.reason


def test_the_judge_mints_one_grant_per_approval(tmp_path: Path) -> None:
    said = owner("yes post it", ident="ask:toolu_9#0")
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),))
    first = grants.check(event(tmp_path, "first", allow=True, reason="approved", relied_on=["ask:toolu_9#0"]))
    assert isinstance(first, Allowed) and first.grant.source_key == "ask:toolu_9#0"
    assert first.grant.approved == {"text": "first"}
    second = grants.check(
        event(tmp_path, "second", call="c2", allow=True, reason="approved", relied_on=["ask:toolu_9#0"])
    )
    assert isinstance(second, Denied) and f"Grant {first.grant.id} was spent" in second.explained


def test_a_counted_approval_binds_its_budget_not_the_first_text(tmp_path: Path) -> None:
    said = owner("send these three replies in that thread")
    grants = declared(rules=(ContentMatches(),), judge=Judge("rules"), evidence=(Fixed((said,)),))
    verdict = {"allow": True, "reason": "three replies", "relied_on": [said.id], "uses": 3}
    first = grants.check(event(tmp_path, "one", **verdict))
    assert isinstance(first, Allowed) and first.grant.uses == 3 and first.grant.approved is None
    second = event(tmp_path, "two", call="c2", **verdict)
    allowed = grants.check(second)
    assert isinstance(allowed, Allowed) and allowed.grant.id == first.grant.id and allowed.remaining == 1
    assert second.ctx.call_llm.call_count == 1
    assert "- one" not in str(second.ctx.call_llm.call_args.args[0])


def test_racing_judges_share_one_approval(tmp_path: Path) -> None:
    said = owner("yes post it", ident="ask:toolu_9#0")
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),))
    results: list[Any] = []
    barrier = threading.Barrier(6)

    def race(n: int) -> None:
        evt = event(tmp_path / str(n), f"text {n}", call=f"c{n}", allow=True, reason="ok", relied_on=["ask:toolu_9#0"])
        barrier.wait()
        results.append(grants.check(evt))

    for n in range(6):
        (tmp_path / str(n)).mkdir()
    threads = [threading.Thread(target=race, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(bool(result) for result in results) == 1
    assert len(store.grants("test.write", TREE)) == 1


def test_a_rule_denies_a_grant_minted_from_evidence(tmp_path: Path) -> None:
    said = owner("post anything")
    never = Never("never", lambda action: True, "this kind never goes out", always=True)
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),), rules=(never,))
    denied = grants.check(event(tmp_path, "one", allow=True, reason="r", relied_on=[said.id]))
    assert isinstance(denied, Denied) and "this kind never goes out" in denied.reason


def test_verbatim_standing_words_mint_an_unlimited_grant(tmp_path: Path) -> None:
    said = owner(f"ok {STANDING} thanks")
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),), standing_rules=("no-edit",))
    allowed = grants.check(
        event(tmp_path, "one", allow=True, reason="standing", relied_on=[said.id], standing=STANDING)
    )
    assert isinstance(allowed, Allowed) and allowed.grant.standing and allowed.remaining is None
    assert allowed.grant.rules == ["no-edit"] and allowed.grant.evidence[0].quote == STANDING
    again = grants.check(event(tmp_path, "two", call="c2", allow=True, reason="still standing"))
    assert isinstance(again, Allowed) and again.grant.id == allowed.grant.id


def test_standing_words_mint_one_grant_per_destination_they_cover(tmp_path: Path) -> None:
    said = owner("reply in any alert thread without asking")
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),))
    standing = {"allow": True, "reason": "standing", "relied_on": [said.id], "standing": said.quote}
    first = grants.check(event(tmp_path, "one", **standing))
    other = Proposal(scope={"channel": "C1", "thread": "3.4"}, payload={"text": "two"}, summary="reply in C1/3.4")
    second = grants.check(event(tmp_path, "two", call="c2", **standing), other)
    assert isinstance(first, Allowed) and isinstance(second, Allowed)
    assert second.grant.id != first.grant.id and second.grant.scope == other.scope
    assert second.remaining is None


def test_a_ruling_the_judge_relies_on_covers_each_action_in_its_class(tmp_path: Path) -> None:
    ruling = Evidence(
        id="ccn:543e865", source="ccn-answer", quote="reply in any alert thread", key="ccn:543e865@r", live=True
    )
    grants = declared(judge=Judge("rules"), evidence=(Fixed((ruling,)),), standing_rules=("no-edit",))
    first = grants.check(event(tmp_path, "mechanism", allow=True, reason="ruled", relied_on=[ruling.id]))
    assert isinstance(first, Allowed) and first.remaining is None and first.grant.rules == ["no-edit"]
    again = grants.check(event(tmp_path, "fix live", call="c2", allow=True, reason="ruled", relied_on=[ruling.id]))
    assert isinstance(again, Allowed) and again.grant.id == first.grant.id
    other = Proposal(scope={"channel": "C1", "thread": "3.4"}, payload={"text": "x"}, summary="reply in C1/3.4")
    elsewhere = grants.check(event(tmp_path, "x", call="c3", allow=True, reason="ruled", relied_on=[ruling.id]), other)
    assert isinstance(elsewhere, Allowed) and elsewhere.grant.scope == other.scope


@pytest.mark.parametrize("cite", ["543e865", "[ccn:543e865]", " ccn:543e865 "])
def test_a_cited_id_counts_without_its_prefix_or_brackets(tmp_path: Path, cite: str) -> None:
    ruling = Evidence(id="ccn:543e865", source="ccn-answer", quote="ok", key="ccn:543e865@r", live=True)
    grants = declared(judge=Judge("rules"), evidence=(Fixed((ruling,)),))
    assert isinstance(grants.check(event(tmp_path, allow=True, reason="ruled", relied_on=[cite])), Allowed)


def test_the_judge_leaves_the_transcript_open_for_a_second_judgement(tmp_path: Path) -> None:
    minted(approved={"text": "the approved text"})
    said = owner("yes post it", ident="ask:toolu_9#0")
    evt = event(tmp_path, "changed", allow=False, reason="not this text")
    declared(rules=(ContentMatches(),), judge=Judge("rules"), evidence=(Fixed((said,)),)).check(evt)
    assert evt.ctx.call_llm.call_count == 2
    assert all(call.kwargs["evidence"] is False for call in evt.ctx.call_llm.call_args_list)


def test_standing_words_the_owner_never_said_mint_one_use(tmp_path: Path) -> None:
    said = owner("post this one")
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),))
    allowed = grants.check(event(tmp_path, "one", allow=True, reason="r", relied_on=[said.id], standing=STANDING))
    assert isinstance(allowed, Allowed) and allowed.grant.uses == 1


def test_a_judge_that_gives_no_verdict_denies(tmp_path: Path) -> None:
    evt = event(tmp_path)
    evt.ctx.call_llm = MagicMock(side_effect=TimeoutError())  # type: ignore[method-assign]
    denied = declared(judge=Judge("rules"), evidence=(Fixed((owner("x"),)),)).check(evt)
    assert isinstance(denied, Denied) and "no verdict" in denied.reason and denied.undecided


def test_a_denied_event_releases_its_reservation(tmp_path: Path) -> None:
    grant = minted()
    with reservations() as reserved:
        assert declared().check(event(tmp_path, call="toolu_r"))
        assert not declared().check(event(tmp_path, "other", call="toolu_s"))
        settle(reserved, allowed=False)
    assert [spend.state for spend in store.spends(grant.id)] == ["released"]
    with reservations() as reserved:
        assert declared().check(event(tmp_path, call="toolu_t"))
        settle(reserved, allowed=True)
    assert [spend.state for spend in store.spends(grant.id)] == ["released", "committed"]


def test_spends_are_atomic(tmp_path: Path) -> None:
    grant = minted()
    won: list[int | None] = []
    barrier = threading.Barrier(8)

    def spend(n: int) -> None:
        barrier.wait()
        try:
            won.append(
                store.reserve(
                    grant.id,
                    tree=TREE,
                    scope=SCOPE,
                    state="committed",
                    session="s",
                    agent="main",
                    tool_use_id=f"c{n}",
                    fingerprint=f"f{n}",
                    summary="s",
                    reason="r",
                    relied_on=[],
                )
            )
        except store.SpentError:
            pass

    threads = [threading.Thread(target=spend, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert won == [0]


def test_rulings_count_only_when_written_before_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)
    answers = [
        {
            "id": "543e865aaaa",
            "title": "alerts",
            "body": "C1: not-ours replies ok",
            "updated_at": "2026-10-01T10:00:00Z",
        },
        {
            "id": "777beefaaaa",
            "title": "self-granted",
            "body": "C1: anything goes",
            "updated_at": "2026-10-02T18:30:00Z",
        },
    ]
    monkeypatch.setattr(evidence_module, "ccn_answers", lambda evt, term: answers)
    items = Rulings(search=lambda action: action.scope["channel"], started=lambda evt: started).collect(
        event(tmp_path), Proposal(scope=SCOPE)
    )
    assert [item.id for item in items] == ["ccn:543e865"]
    assert items[0].key == "ccn:543e865aaaa@2026-10-01T10:00:00+00:00"
    assert items[0].live


def test_standing_rulings_read_each_id_written_before_the_session_and_skip_absent_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)
    answers = {
        "c9b27c1": {"id": "c9b27c1aaaa", "title": "gc", "body": "close settled", "updated_at": "2026-10-01T10:00:00Z"},
        "777beef": {"id": "777beefaaaa", "title": "late", "body": "close all", "updated_at": "2026-10-02T18:30:00Z"},
    }

    def show(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        found = answers.get(argv[3])
        if found is None:
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr="not-found")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(found), stderr="")

    monkeypatch.setattr(evidence_module.subprocess, "run", show)
    standing = StandingRulings(("c9b27c1", "777beef", "6190a4a"), started=lambda evt: started)
    items = standing.collect(event(tmp_path), Proposal(scope={}))
    assert [(item.id, item.key, item.live) for item in items] == [
        ("ccn:c9b27c1", "ccn:c9b27c1aaaa@2026-10-01T10:00:00+00:00", True)
    ]


def test_standing_rulings_fail_on_an_unreadable_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="ccn: no repository"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        StandingRulings(("c9b27c1",)).collect(event(tmp_path), Proposal(scope={}))


def test_a_ruling_names_a_term_only_as_a_whole_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {
        "id": "543e865aaaa",
        "title": "close",
        "body": "close term_ab and com.x.helper, not term_a-2.",
        "updated_at": "2026-10-01T10:00:00Z",
    }
    monkeypatch.setattr(evidence_module, "ccn_answers", lambda evt, term: [answer])
    rulings = Rulings(search=lambda action: action.scope["terminal"], started=lambda evt: store.now())
    for unnamed in ("term_a", "com.x"):
        assert rulings.collect(event(tmp_path), Proposal(scope={"terminal": unnamed})) == []
    for named in ("term_ab", "com.x.helper", "term_a-2"):
        assert rulings.collect(event(tmp_path), Proposal(scope={"terminal": named}))


def entry(grants: Grants) -> RegisteredHook:
    return RegisteredHook(
        spec=HookSpec(events=Event.PreToolUse, message="Needs the owner's permission.", block=True, grants=grants),
        name="needs_grant",
    )


def test_a_hook_with_grants_lifts_its_block_for_a_covering_grant(tmp_path: Path) -> None:
    grant = minted()
    result = execute_hook(entry(declared()), event(tmp_path))
    assert result == HookResult(
        action=Action.warn, message=f"needs_grant: allowed by grant `{grant.id}`, 0 use(s) left.", approve=False
    )
    assert result is not None and not result_violations(result)


def test_a_hook_with_grants_keeps_its_block_and_tells_the_user_why(tmp_path: Path) -> None:
    result = execute_hook(entry(declared()), event(tmp_path))
    assert result == HookResult(
        action=Action.block,
        message="No test.write grant covers reply in C1/1.2. Ask the user for permission for exactly this action.",
        system_message="needs_grant: No test.write grant covers reply in C1/1.2."
        " Ask the user for permission for exactly this action.",
    )


def test_a_failing_grant_check_keeps_the_block(tmp_path: Path) -> None:
    broken = Grants("test.write", ("channel", "thread"), lambda evt: Proposal(scope={}))
    result = execute_hook(entry(broken), event(tmp_path))
    assert result is not None and result.action is Action.block
    assert result.message == "Needs the owner's permission."
    assert "The grant check failed (ValueError" in (result.system_message or "")


def test_a_hook_with_grants_fails_closed() -> None:
    from captain_hook import hook
    from captain_hook.app import GRANTS_INCOMPLETE, _state

    hook(Event.PreToolUse, "Needs the owner's permission.", block=True, grants=declared())
    assert _state.hooks[-1].spec.on_incomplete == GRANTS_INCOMPLETE


def test_hook_rejects_grants_without_a_block() -> None:
    from captain_hook import hook

    with pytest.raises(ValueError, match="grants needs block=True"):
        hook(Event.PreToolUse, "m", grants=declared())


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        ({"hookSpecificOutput": {"permissionDecision": "deny"}}, True),
        ({"hookSpecificOutput": {"decision": {"behavior": "deny"}}}, True),
        ({"decision": "block", "reason": "r"}, True),
        ({"hookSpecificOutput": {"permissionDecision": "allow"}}, False),
        (None, False),
        ("plain text", False),
    ],
)
def test_denies_reads_every_deny_shape(envelope: Any, expected: bool) -> None:
    assert denies(envelope) is expected


def test_the_cli_mints_lists_shows_and_revokes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", TREE)
    monkeypatch.setattr(grant_cli_module, "owner_at_terminal", lambda: None)
    runner = CliRunner()
    added = runner.invoke(
        grant_cli,
        [
            "add",
            "--kind",
            "test.write",
            "--scope",
            "channel=C1",
            "--scope",
            "thread=1.2",
            "--unlimited",
            "--for",
            "7d",
            "--quote",
            STANDING,
        ],
    )
    assert added.exit_code == 0, added.output
    grant_id = added.output.split()[0]
    found = store.load(grant_id)
    assert found.tree == TREE and found.scope == SCOPE and found.uses is None and found.evidence[0].quote == STANDING
    assert found.expires is not None and found.expires - store.now() > timedelta(days=6)
    assert grant_id in runner.invoke(grant_cli, ["list"]).output
    assert runner.invoke(grant_cli, ["revoke", grant_id]).exit_code == 0
    assert grant_id not in runner.invoke(grant_cli, ["list"]).output
    assert "revoked" in runner.invoke(grant_cli, ["list", "--all"]).output


def test_the_cli_refuses_to_mint_without_a_terminal() -> None:
    result = CliRunner().invoke(grant_cli, ["add", "--kind", "k", "--scope", "a=b", "--quote", "q", "--tree", "t"])
    assert result.exit_code != 0 and "only the owner mints a grant" in result.output


def test_the_cli_rejects_a_malformed_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grant_cli_module, "owner_at_terminal", lambda: None)
    result = CliRunner().invoke(grant_cli, ["add", "--kind", "k", "--scope", "nope", "--quote", "q", "--tree", "t"])
    assert result.exit_code != 0 and "is not key=value" in result.output


def test_an_uncited_allow_mints_nothing(tmp_path: Path) -> None:
    grants = declared(judge=Judge("rules"), evidence=(Fixed((owner("yes"),)),))
    denied = grants.check(event(tmp_path, allow=True, reason="looks fine"))
    assert isinstance(denied, Denied) and "without citing" in denied.reason
    assert store.grants("test.write", TREE) == []


def test_an_approval_spent_on_one_destination_never_covers_another(tmp_path: Path) -> None:
    said = owner("yes post it", ident="ask:toolu_9#0")
    other = Grants(
        "test.write",
        ("channel", "thread"),
        lambda evt: Proposal(scope={"channel": "C9", "thread": ""}, payload={"text": "x"}),
    )
    first = declared(judge=Judge("rules"), evidence=(Fixed((said,)),))
    assert first.check(event(tmp_path, "one", allow=True, reason="ok", relied_on=[said.id]))
    second = Grants(
        "test.write", ("channel", "thread"), other.action, judge=Judge("rules"), evidence=(Fixed((said,)),)
    ).check(event(tmp_path, "one", call="c2", allow=True, reason="ok", relied_on=[said.id]))
    assert isinstance(second, Denied) and "already went to grant" in second.explained


def test_an_edited_ruling_stops_its_grant(tmp_path: Path) -> None:
    ruling = Evidence(
        id="ccn:543e865", source="ccn-answer", quote="ok", key="ccn:543e865@2026-10-01T10:00:00+00:00", live=True
    )
    minted(uses=None, evidence=[ruling])
    edited = ruling.model_copy(update={"key": "ccn:543e865@2026-10-02T10:00:00+00:00"})
    denied = declared(judge=Judge("rules"), evidence=(Fixed((edited,)),)).check(
        event(tmp_path, allow=False, reason="No ruling covers it.")
    )
    assert isinstance(denied, Denied) and "changed after the grant was minted" in denied.explained
    assert declared(evidence=(Fixed((ruling,)),)).check(event(tmp_path, call="c2"))


def test_a_withdrawal_revokes_the_grant_for_good(tmp_path: Path) -> None:
    grant = minted(uses=None)
    later = owner("stop replying there", at=store.now())
    grants = declared(judge=Judge("rules"), evidence=(Fixed((later,)),))
    denied = grants.check(event(tmp_path, allow=False, reason="withdrawn", withdrawn=True))
    assert isinstance(denied, Denied)
    assert store.load(grant.id).revoked is not None


def test_a_late_settle_releases_and_reports_the_lost_use(tmp_path: Path) -> None:
    grant = minted()
    with reservations() as reserved:
        assert declared().check(event(tmp_path, call="late"))
    with store.connect() as db:
        db.execute("UPDATE spends SET at = ?", ((store.now() - timedelta(minutes=5)).isoformat(),))
    assert settle(reserved, allowed=True) is False
    assert [spend.state for spend in store.spends(grant.id)] == ["released"]


def test_a_judgeless_declaration_mints_one_use_from_the_evidence_that_names_the_action(tmp_path: Path) -> None:
    ruling = Evidence(id="ccn:543e865", source="ccn-answer", quote="close C1", key="ccn:543e865@r", live=True)
    grants = Grants("test.close", ("channel", "thread"), evidence=(Fixed((ruling,)),))
    allowed = grants.check(event(tmp_path), Proposal(scope=SCOPE, summary="close C1"))
    assert isinstance(allowed, Allowed) and allowed.remaining == 0
    assert allowed.grant.source_key == f"{ruling.key}#{json.dumps(SCOPE)}" and allowed.grant.uses == 1
    assert [spend.relied_on for spend in store.spends(allowed.grant.id)] == [["ccn:543e865"]]
    denied = grants.check(event(tmp_path, call="c2"), Proposal(scope=SCOPE, payload={"tab": True}, summary="again"))
    assert isinstance(denied, Denied) and "was spent" in denied.explained


def test_one_judgeless_approval_covers_each_scope_it_names_once_across_trees(tmp_path: Path) -> None:
    ruling = Evidence(id="ccn:543e865", source="ccn-answer", quote="close C1 and C2", key="ccn:543e865@r", live=True)
    grants = Grants("test.close", ("channel", "thread"), evidence=(Fixed((ruling,)),))
    first = grants.check(event(tmp_path), Proposal(scope=SCOPE))
    second = grants.check(event(tmp_path, call="c2"), Proposal(scope=SCOPE | {"channel": "C2"}))
    assert isinstance(first, Allowed) and isinstance(second, Allowed) and first.grant.id != second.grant.id
    elsewhere = grants.check(event(tmp_path, session="other-root", call="c3"), Proposal(scope=SCOPE, summary="x"))
    assert isinstance(elsewhere, Denied) and "which does not include this action" in elsewhere.explained


def test_a_replay_window_bounds_free_retries_of_a_one_use_grant(tmp_path: Path) -> None:
    minted()
    assert declared(replay=timedelta(minutes=2)).check(event(tmp_path, "one"))
    assert declared(replay=timedelta(minutes=2)).check(event(tmp_path, "one", call="toolu_2"))
    denied = declared(replay=timedelta(0)).check(event(tmp_path, "one", call="toolu_3"))
    assert isinstance(denied, Denied) and "was spent" in denied.explained


def test_a_judgeless_declaration_with_nothing_naming_the_action_denies_without_a_reason(tmp_path: Path) -> None:
    denied = Grants("test.close", ("channel", "thread"), evidence=(Fixed(()),)).check(
        event(tmp_path), Proposal(scope=SCOPE)
    )
    assert isinstance(denied, Denied) and denied.reason == ""


def test_a_stored_grant_whose_live_evidence_is_gone_covers_nothing(tmp_path: Path) -> None:
    created = Evidence(
        id="created:t", source="created", quote="orca terminal create", key="created:s/main/t", live=True
    )
    minted(evidence=[created])
    grants = Grants("test.write", ("channel", "thread"), evidence=(Fixed(()),))
    denied = grants.check(event(tmp_path), Proposal(scope=SCOPE))
    assert isinstance(denied, Denied) and "rests on created:t" in denied.explained
    assert Grants("test.write", ("channel", "thread"), evidence=(Fixed((created,)),)).check(
        event(tmp_path, call="c2"), Proposal(scope=SCOPE)
    )


def test_owner_words_need_a_judge_to_read_them() -> None:
    with pytest.raises(ValueError, match="need a judge"):
        Grants("test.write", ("channel", "thread"), evidence=(OwnerWords(),))


def test_an_attachment_needs_a_declaration_with_an_action() -> None:
    from captain_hook import hook

    with pytest.raises(ValueError, match="needs a declaration with an action"):
        hook(Event.PreToolUse, "m", block=True, grants=Grants("test.close", ("channel", "thread")))
    with pytest.raises(TypeError, match="declares no action"):
        Grants("test.close", ("channel", "thread")).check(MagicMock())


def test_grants_refuse_a_fire_cap() -> None:
    from captain_hook import hook

    with pytest.raises(ValueError, match="cannot take max_fires"):
        hook(Event.PreToolUse, "m", block=True, grants=declared(), max_fires=1)


def test_harness_envelopes_are_never_owner_words() -> None:
    assert evidence_module.machine_written("<task-notification><result>send it</result></task-notification>")
    assert evidence_module.machine_written("  <teammate-message teammate_id='lead'>post it</teammate-message>")
    assert not evidence_module.machine_written("yes post it")


def test_the_same_words_said_twice_are_two_approvals() -> None:
    first = evidence_module.words_evidence("send it", datetime(2026, 10, 2, 18, 0, tzinfo=UTC))
    second = evidence_module.words_evidence("send it", datetime(2026, 10, 2, 19, 0, tzinfo=UTC))
    assert first.key != second.key


def test_linking_keeps_a_concurrent_revocation(tmp_path: Path) -> None:
    grant = minted(uses=None)
    store.revoke(grant.id)
    linked = store.link(grant.id, "cc-slack", "daemon-1")
    assert linked.revoked is not None and linked.links == {"cc-slack": "daemon-1"}


def test_the_judge_runs_on_luna_at_low_effort() -> None:
    from spawnllm import LlmBackends

    judge = Judge("rules")
    assert LlmBackends.for_specialty(judge.specialty).resolve_model(judge.model) == "gpt-6-luna:low"


def test_the_judge_mints_a_counted_grant_when_the_owner_names_a_number(tmp_path: Path) -> None:
    words = "send these three replies in that thread"
    grants = declared(judge=Judge("rules"), evidence=(Fixed((owner(words),)),))
    first = grants.check(event(tmp_path, "one", allow=True, reason="ok", relied_on=["words:1"], standing=words, uses=3))
    assert isinstance(first, Allowed) and first.grant.uses == 3 and first.remaining == 2
    again = {"allow": True, "reason": "ok", "relied_on": ["words:1"]}
    assert [bool(grants.check(event(tmp_path, f"t{n}", call=f"c{n}", **again))) for n in range(3)] == [
        True,
        True,
        False,
    ]


def test_an_adopted_grant_covers_the_adopting_tree_and_shares_its_budget(tmp_path: Path) -> None:
    grant = minted(uses=2)
    assert not declared().check(event(tmp_path, "lane", session="lane-root"))
    adoption = store.adopt(grant.id, tree="lane-root", session="lane-root", agent="comms")
    assert adoption.agent == "comms" and [found.tree for found in store.adoptions(grant.id)] == ["lane-root"]
    assert declared().check(event(tmp_path, "lane", session="lane-root", call="c1"))
    assert declared().check(event(tmp_path, "root", call="c2"))
    assert not declared().check(event(tmp_path, "again", session="lane-root", call="c3"))


def test_a_downstream_spender_names_the_grant_and_pays_through_the_cli(tmp_path: Path) -> None:
    grant = minted(uses=1)
    named = declared(spent_by="cc-slack").check(event(tmp_path, "one"))
    assert isinstance(named, Allowed) and named.grant.id == grant.id and store.spends(grant.id) == []
    argv = ["spend", grant.id, "--scope", "channel=C1", "--scope", "thread=1.2", "--tree", TREE]
    argv += ["--session", TREE, "--call", "post-1", "--fingerprint", "f1", "--summary", "reply"]
    paid = CliRunner().invoke(grant_cli, argv)
    assert paid.exit_code == 0 and '"remaining": 0' in paid.output
    assert [spend.state for spend in store.spends(grant.id)] == ["committed"]
    refused = CliRunner().invoke(
        grant_cli, [*argv[:-6], "--call", "post-2", "--fingerprint", "f2", "--summary", "reply"]
    )
    assert refused.exit_code == 1 and f"Grant {grant.id} was spent at" in refused.output
    assert isinstance(declared(spent_by="cc-slack").check(event(tmp_path, "two", call="c2")), Denied)


def test_a_downstream_spender_releases_the_use_of_a_write_that_never_happened(tmp_path: Path) -> None:
    grant = minted(uses=1)
    argv = ["spend", grant.id, "--scope", "channel=C1", "--scope", "thread=1.2", "--tree", TREE]
    argv += ["--session", TREE, "--call", "post-1", "--fingerprint", "f1", "--summary", "reply"]
    assert CliRunner().invoke(grant_cli, argv).exit_code == 0
    released = CliRunner().invoke(grant_cli, ["release", grant.id, "--call", "post-1"])
    assert released.exit_code == 0 and '"released": 1' in released.output and '"remaining": 1' in released.output
    assert [spend.state for spend in store.spends(grant.id)] == ["released"]
    again = CliRunner().invoke(grant_cli, [*argv[:-6], "--call", "post-2", "--fingerprint", "f2", "--summary", "reply"])
    assert again.exit_code == 0 and '"remaining": 0' in again.output
    assert '"released": 0' in CliRunner().invoke(grant_cli, ["release", grant.id, "--call", "post-1"]).output


def test_a_downstream_spend_refuses_another_tree(tmp_path: Path) -> None:
    grant = minted()
    argv = ["spend", grant.id, "--scope", "channel=C1", "--scope", "thread=1.2", "--tree", "elsewhere"]
    argv += ["--session", "elsewhere", "--call", "p", "--fingerprint", "f", "--summary", "reply"]
    refused = CliRunner().invoke(grant_cli, argv)
    assert refused.exit_code == 1 and "which does not include this action" in refused.output


def test_a_downstream_spender_names_a_spent_one_shot_for_the_same_payload_again(tmp_path: Path) -> None:
    grant = minted()
    assert declared().check(event(tmp_path, "same"))
    again = declared(spent_by="cc-slack").check(event(tmp_path, "same", call="toolu_2"))
    assert isinstance(again, Allowed) and again.grant.id == grant.id
    assert isinstance(declared(spent_by="cc-slack").check(event(tmp_path, "other", call="toolu_3")), Denied)


def test_spending_an_unknown_grant_names_it(tmp_path: Path) -> None:
    argv = ["spend", "000000000000", "--scope", "channel=C1", "--scope", "thread=1.2", "--tree", TREE]
    refused = CliRunner().invoke(
        grant_cli, [*argv, "--session", TREE, "--call", "p", "--fingerprint", "f", "--summary", "s"]
    )
    assert refused.exit_code == 1 and "No grant 000000000000 exists" in refused.output


def recorded_words(quote: str, *, expires: datetime, ago: timedelta = timedelta(days=2)) -> Grant:
    said = evidence_module.words_evidence(quote, store.now() - ago)
    return store.mint(
        Grant(
            id=store.new_id(),
            kind="words",
            tree=TREE,
            scope={},
            evidence=[said],
            source_key=said.key,
            expires=expires,
            author="words@test",
            created=store.now() - ago,
        )
    )


def test_a_lane_reads_its_roots_older_words_from_the_recorded_index(tmp_path: Path) -> None:
    recorded_words("post the AIG summary in that thread", expires=store.now() + timedelta(days=5))
    recorded_words("post anything anywhere", expires=store.now() - timedelta(minutes=1))
    lane = event(tmp_path, session="lane-session")
    lane.ctx.root_path = tmp_path / f"{TREE}.jsonl"
    quotes = [item.quote for item in OwnerWords().collect(lane, proposal(lane))]
    assert quotes == ["post the AIG summary in that thread"]


def in_orca(monkeypatch: pytest.MonkeyPatch, handle: str) -> None:
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", handle)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "1")


def fake_orca(monkeypatch: pytest.MonkeyPatch, status: list[str] | None = None) -> list[str]:
    calls: list[str] = []
    lane = status or ["dispatched"]

    def resolve(handle: str) -> tuple[str | None, str | None]:
        calls.append(handle)
        return ("run_1", "term_root") if handle == "term_lane" and lane[0] == "dispatched" else (None, None)

    monkeypatch.setattr(orca_module, "resolve", resolve)
    return calls


def test_an_attended_orca_session_records_its_terminal_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    in_orca(monkeypatch, "term_root")
    evt = event(tmp_path)
    orca_module.record_terminal(evt)
    orca_module.record_terminal(evt)
    assert store.terminal_tree("term_root") == TREE
    assert not store.record_terminal("term_root", TREE)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "0")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_nested")
    orca_module.record_terminal(event(tmp_path / "nested", session="nested-print"))
    assert store.terminal_tree("term_nested") is None


def test_an_orca_lane_spends_its_coordinators_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.record_terminal("term_root", TREE)
    grant = minted(uses=2)
    calls = fake_orca(monkeypatch)
    in_orca(monkeypatch, "term_lane")
    lane = event(tmp_path / "lane", "lane", session="lane-root")
    assert declared().check(lane)
    assert [(found.tree, found.agent) for found in store.adoptions(grant.id)] == [("lane-root", "orca:run_1")]
    assert declared().check(event(tmp_path / "lane", "again", session="lane-root", call="c2"))
    assert calls == ["term_lane"]
    assert not declared().check(event(tmp_path, "root", call="c3"))


def test_no_orca_binds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.record_terminal("term_root", TREE)
    minted(uses=None)
    calls = fake_orca(monkeypatch)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    assert not declared().check(event(tmp_path, session="lane-root"))
    in_orca(monkeypatch, "term_unknown")
    assert not declared().check(event(tmp_path / "other", session="other-root", call="c2"))
    assert calls == ["term_unknown"]


def test_an_orca_lane_never_adopts_the_coordinators_recorded_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.record_terminal("term_root", TREE)
    words = recorded_words("send one reply in that thread", expires=store.now() + timedelta(days=5))
    grant = minted(uses=1)
    fake_orca(monkeypatch)
    in_orca(monkeypatch, "term_lane")
    assert declared().check(event(tmp_path / "a", "a", session="lane-a"))
    assert store.adoptions(words.id) == [] and [found.tree for found in store.adoptions(grant.id)] == ["lane-a"]
    judged = declared(judge=Judge("rules"), evidence=(OwnerWords(),))
    lane_b = event(tmp_path / "b", "b", session="lane-b", call="c2", allow=True, reason="ok", relied_on=[words.id])
    assert evidence_module.recorded(lane_b, "words") == []
    assert not judged.check(lane_b)
    assert not declared().check(event(tmp_path, "root", call="c3"))


def test_a_reused_coordinator_terminal_never_lends_the_new_sessions_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.record_terminal("term_root", TREE)
    minted(uses=None)
    fake_orca(monkeypatch)
    in_orca(monkeypatch, "term_lane")
    assert declared().check(event(tmp_path / "lane", "lane", session="lane-root"))
    store.record_terminal("term_root", "unrelated-root")
    unrelated = store.mint(
        Grant(id=store.new_id(), kind="test.write", tree="unrelated-root", scope=SCOPE, author="t", created=store.now())
    )
    monkeypatch.setattr(orca_module, "BINDING_TTL", timedelta(0))
    declared().check(event(tmp_path / "lane", "again", session="lane-root", call="c2"))
    assert store.adoptions(unrelated.id) == []


def test_an_orca_binding_is_revalidated_after_its_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.record_terminal("term_root", TREE)
    first = minted(uses=None)
    status = ["dispatched"]
    calls = fake_orca(monkeypatch, status)
    in_orca(monkeypatch, "term_lane")
    assert declared().check(event(tmp_path / "lane", "lane", session="lane-root"))
    assert [found.tree for found in store.adoptions(first.id)] == ["lane-root"]
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_moved")
    declared().check(event(tmp_path / "lane", "moved", session="lane-root", call="c2"))
    assert calls == ["term_lane", "term_moved"]
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_lane")
    monkeypatch.setattr(orca_module, "BINDING_TTL", timedelta(0))
    status[0] = "completed"
    later = minted(uses=None)
    declared().check(event(tmp_path / "lane", "done", session="lane-root", call="c3"))
    assert store.adoptions(later.id) == []


def test_a_withdrawal_whose_record_expired_still_reaches_the_judge(tmp_path: Path) -> None:
    grant = minted(approved={"text": "ok"}, uses=None)
    recorded_words("stop posting there", expires=store.now() - timedelta(seconds=1), ago=timedelta(seconds=30))
    evt = event(tmp_path, "ok", allow=False, reason="the owner withdrew it", withdrawn=True)
    denied = declared(rules=(ContentMatches(),), judge=Judge("rules"), evidence=(OwnerWords(),)).check(evt)
    assert isinstance(denied, Denied) and "withdrew" in denied.reason
    assert "stop posting there" in evt.ctx.call_llm.call_args_list[0].args[0].system_text
    assert store.load(grant.id).revoked is not None


@pytest.mark.parametrize(
    ("status", "expected"),
    [("dispatched", ("run_inline", "term_root")), ("completed", (None, None))],
    ids=["dispatched", "settled"],
)
def test_a_lane_resolves_its_run_through_orcas_worker_list(status: str, expected: tuple[str | None, ...]) -> None:
    from captain_hook.builtin_packs.general.hooks._sessions import inline_run, inline_worker
    from captain_hook.testing.helpers import stubbed_commands

    commands = {"orca orchestration worker-list": inline_worker("term_lane", status), **inline_run("term_root")}
    with stubbed_commands(commands):
        assert orca_module.resolve("term_lane") == expected
        assert orca_module.resolve("term_other") == (None, None)
