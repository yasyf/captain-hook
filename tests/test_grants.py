from __future__ import annotations

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
    Proposal,
    Rulings,
    reservations,
    settle,
    store,
)
from captain_hook.grants import evidence as evidence_module
from captain_hook.grants.cli import grant as grant_cli
from captain_hook.types import Action, HookSpec, RegisteredHook
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
    assert f"grant {grant.id} was spent at" in second.reason
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
    assert isinstance(denied, Denied) and "expired at" in denied.reason


def test_a_revoked_grant_covers_nothing(tmp_path: Path) -> None:
    grant = minted(uses=None)
    store.revoke(grant.id)
    assert not declared().check(event(tmp_path))
    assert "revoked" in store.unusable(store.load(grant.id), [], store.now())  # type: ignore[operator]


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
    assert isinstance(second, Denied) and f"grant {first.grant.id} was spent" in second.reason


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


def test_standing_words_the_owner_never_said_mint_one_use(tmp_path: Path) -> None:
    said = owner("post this one")
    grants = declared(judge=Judge("rules"), evidence=(Fixed((said,)),))
    allowed = grants.check(event(tmp_path, "one", allow=True, reason="r", relied_on=[said.id], standing=STANDING))
    assert isinstance(allowed, Allowed) and allowed.grant.uses == 1


def test_a_judge_that_gives_no_verdict_denies(tmp_path: Path) -> None:
    evt = event(tmp_path)
    evt.ctx.call_llm = MagicMock(side_effect=TimeoutError())  # type: ignore[method-assign]
    denied = declared(judge=Judge("rules"), evidence=(Fixed((owner("x"),)),)).check(evt)
    assert isinstance(denied, Denied) and "no verdict" in denied.reason


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
            "created_at": "2026-10-01T10:00:00+00:00",
        },
        {
            "id": "777beefaaaa",
            "title": "self-granted",
            "body": "C1: anything goes",
            "created_at": "2026-10-01T10:00:00+00:00",
            "updated_at": "2026-10-02T18:30:00+00:00",
        },
    ]
    monkeypatch.setattr(evidence_module, "ccn_answers", lambda evt, term: answers)
    items = Rulings(search=lambda action: action.scope["channel"], started=lambda evt: started).collect(
        event(tmp_path), Proposal(scope=SCOPE)
    )
    assert [item.id for item in items] == ["ccn:543e865"]
    assert items[0].key == "ccn:543e865aaaa@2026-10-01T10:00:00+00:00"


def entry(grants: Grants) -> RegisteredHook:
    return RegisteredHook(
        spec=HookSpec(events=Event.PreToolUse, message="Needs the owner's permission.", block=True, grants=grants),
        name="needs_grant",
    )


def test_a_hook_with_grants_lifts_its_block_for_a_covering_grant(tmp_path: Path) -> None:
    grant = minted()
    result = execute_hook(entry(declared()), event(tmp_path))
    assert result is not None and result.action is Action.warn
    assert f"allowed by grant {grant.id}, 0 use(s) left" in (result.message or "")


def test_a_hook_with_grants_keeps_its_block_and_says_why(tmp_path: Path) -> None:
    result = execute_hook(entry(declared()), event(tmp_path))
    assert result is not None and result.action is Action.block
    assert result.message == (
        "Needs the owner's permission.\n\nNo test.write grant covers reply in C1/1.2."
        " Ask the user for permission for exactly this action."
    )


def test_a_failing_grant_check_keeps_the_block(tmp_path: Path) -> None:
    broken = Grants("test.write", ("channel", "thread"), lambda evt: Proposal(scope={}))
    result = execute_hook(entry(broken), event(tmp_path))
    assert result is not None and result.action is Action.block
    assert "The grant check failed (ValueError" in (result.message or "")


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


def test_the_cli_rejects_a_malformed_scope() -> None:
    result = CliRunner().invoke(grant_cli, ["add", "--kind", "k", "--scope", "nope", "--quote", "q", "--tree", "t"])
    assert result.exit_code != 0 and "is not key=value" in result.output
