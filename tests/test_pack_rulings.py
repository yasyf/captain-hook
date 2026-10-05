from __future__ import annotations

import importlib
import subprocess

import pytest

from captain_hook.builtin_packs.general.hooks import rulings
from captain_hook.builtin_packs.general.hooks.rulings import (
    LEDGER_RULING,
    SLACK_RULING,
    Contradiction,
    Review,
    Ruling,
    RulingsVerdict,
    acks,
    durable_rulings,
    nudge,
    shortlist,
)

LEDGER = Ruling(*LEDGER_RULING)
SLACK = Ruling(*SLACK_RULING)
PROXY = Ruling(
    "1987d4d0000000000000000000000000000000000", "Who owns Platy?", "Go owns Platy; TypeScript adds no verbs."
)


def test_shortlist_ranks_rulings_sharing_rare_terms_with_the_diff() -> None:
    diff = "+\tapplied := ledger.LastApplied(stack)\n+\t// pulumi stack outputs"
    assert shortlist([SLACK, PROXY, LEDGER], diff) == (LEDGER,)


def test_shortlist_caps_the_list() -> None:
    many = [Ruling(f"{n:07x}", f"ledger rule {n}", "the ledger records") for n in range(10)]
    assert len(shortlist([*many, SLACK], "ledger records", size=3)) == 3


def test_acks_read_line_and_comment_forms() -> None:
    message = "release: read pulumi\n\nccx: rules-ack=ec2881e rules-ack=4ffc9a5\n"
    command = "ccx vcs ship -m 'x'  # ccx:rules-ack=1987d4d"
    assert acks([message, command]) == {"ec2881e", "4ffc9a5", "1987d4d"}


def test_acks_ignore_other_keys() -> None:
    assert acks(["ccx: raw role=fix", "git push  # ccx:raw"]) == set()


def test_named_drops_ids_the_shortlist_never_offered() -> None:
    review = Review("diff", (LEDGER,))
    verdict = RulingsVerdict(
        contradictions=[
            Contradiction(ruling="ec2881eeea", sentence="reads the ledger"),
            Contradiction(ruling="2d2c6c8", sentence="not offered"),
            Contradiction(ruling="ec2881e", sentence="  "),
        ]
    )
    assert review.named(verdict) == [(LEDGER, "reads the ledger")]


def test_nudge_shows_the_sentence_to_the_user_without_approving_the_call() -> None:
    result = nudge([(LEDGER, "resolve.go reads the applied commit from the ledger.")])
    assert result.approve is False
    assert "`ec2881e`" in (result.message or "")
    assert "resolve.go reads the applied commit from the ledger." in (result.system_message or "")


def test_durable_rulings_is_empty_when_ccn_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    durable_rulings.cache_clear()
    monkeypatch.setattr(
        rulings.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, stdout="", stderr="not a cc-notes repo"),
    )
    assert durable_rulings(str(tmp_path)) == ()
    durable_rulings.cache_clear()


def test_judge_failure_fails_open() -> None:
    from captain_hook.app import _state
    from captain_hook.dispatch import run_handler
    from captain_hook.testing.helpers import (
        hermetic_request,
        input_to_event,
        isolated_state_root,
        pinned_caches,
        stubbed_commands,
    )
    from captain_hook.testing.types import Input
    from captain_hook.types import Event

    importlib.reload(rulings)
    entry = next(hook for hook in _state.hooks if hook.name.endswith("rules_nudge"))
    key = Input(command="gt submit", commands=rulings.BRANCH, llm={"error": TimeoutError()})
    durable_rulings.cache_clear()
    with (
        isolated_state_root(),
        pinned_caches(),
        hermetic_request(key.env, key.cwd, key.session_id),
        stubbed_commands(key.commands),
    ):
        assert run_handler(entry, input_to_event(Event.PreToolUse, key, "Bash")) is None
    durable_rulings.cache_clear()
