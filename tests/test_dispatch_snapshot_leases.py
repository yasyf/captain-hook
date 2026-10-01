import threading
from unittest.mock import Mock

import pytest

from captain_hook.app import on
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.events import PostToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.transcripts import lazy_transcript
from captain_hook.types import CustomCondition, Event
from captain_hook.util import reqenv


class LeasedFixture:
    def __init__(self, released, name="source"):
        self.released = released
        self.name = name
        self.closed = False
        self.counter = [0]

    def retain(self):
        assert not self.closed
        self.counter[0] += 1
        return LeasedFixture(self.released, str(self.counter[0]))

    def release(self):
        if not self.closed:
            self.closed = True
            self.released.append(self.name)

    def __len__(self):
        assert not self.closed
        return 42


@pytest.fixture
def leased_event(tmp_path, monkeypatch):
    released = []
    loaded = []
    monkeypatch.setattr("captain_hook.snapshots.client.RemoteSession", LeasedFixture)

    def load(path):
        loaded.append(path)
        return LeasedFixture(released)

    source = lazy_transcript("/fixture.jsonl", loader=load)
    evt = PostToolUseEvent(_raw={"tool_name": "Bash"}, ctx=HookContext(SessionStore(tmp_path), source, None))
    return evt, loaded, released


def test_nonreading_hooks_do_not_acquire_source(leased_event):
    evt, loaded, released = leased_event

    @on(Event.PostToolUse)
    def pure(evt):
        return None

    assert dispatch(Event.PostToolUse, evt) is None
    assert loaded == released == []
    assert evt.ctx.transcript.released
    assert evt.ctx.transcript.pins.pending == 0


def test_reading_hooks_have_independent_leases_on_one_lazy_load(leased_event):
    evt, loaded, released = leased_event
    first_released = threading.Event()
    second_borrowed = threading.Event()

    @on(Event.PostToolUse)
    def first(evt):
        assert len(evt.ctx.t) == 42
        assert second_borrowed.wait(timeout=3)
        evt.ctx.t.release()
        first_released.set()

    @on(Event.PostToolUse)
    def second(evt):
        assert len(evt.ctx.t) == 42
        second_borrowed.set()
        assert first_released.wait(timeout=3)
        assert len(evt.ctx.t) == 42

    assert dispatch(Event.PostToolUse, evt) is None
    assert loaded == ["/fixture.jsonl"]
    assert sorted(released) == ["1", "2", "source"]
    assert evt.ctx.transcript.pins.pending == 0


def test_condition_evidence_failure_skips_only_that_hook(leased_event):
    evt, loaded, released = leased_event

    class Incomplete(CustomCondition):
        def check(self, evt):
            assert len(evt.ctx.t) == 42
            raise EvidenceIncomplete("entry_limit", "condition incomplete")

    @on(Event.PostToolUse, only_if=[Incomplete()])
    def first(evt):
        raise AssertionError("handler must not run")

    @on(Event.PostToolUse)
    def second(evt):
        return evt.warn("second still runs")

    overrides = reqenv.RequestOverrides(env={}, cwd="/tmp", client_ppid=1, session_id="s")
    with reqenv.use_request(overrides):
        envelope = dispatch(Event.PostToolUse, evt)
    assert envelope["hookSpecificOutput"]["additionalContext"] == "second still runs"
    assert overrides.evidence_gaps == ["first: entry_limit: condition incomplete"]
    assert sorted(released) == ["1", "source"]
    assert evt.ctx.transcript.pins.pending == 0


def test_handler_evidence_failure_skips_only_that_hook(leased_event):
    evt, loaded, released = leased_event

    @on(Event.PostToolUse)
    def first(evt):
        assert len(evt.ctx.t) == 42
        raise EvidenceIncomplete("output_limit", "handler incomplete")

    @on(Event.PostToolUse)
    def second(evt):
        return evt.warn("second still runs")

    overrides = reqenv.RequestOverrides(env={}, cwd="/tmp", client_ppid=1, session_id="s")
    with reqenv.use_request(overrides):
        envelope = dispatch(Event.PostToolUse, evt)
    assert envelope["hookSpecificOutput"]["additionalContext"] == "second still runs"
    assert overrides.evidence_gaps == ["first: output_limit: handler incomplete"]
    assert evt.ctx.transcript.pins.pending == 0


def test_invalid_evidence_still_fails_the_dispatch(leased_event):
    evt, _, _ = leased_event

    @on(Event.PostToolUse)
    def first(evt):
        raise EvidenceIncomplete("invalid_request", "handler evidence is invalid")

    with pytest.raises(EvidenceIncomplete, match="handler evidence is invalid"):
        dispatch(Event.PostToolUse, evt)
    assert evt.ctx.transcript.pins.pending == 0


def test_context_fork_preserves_model_stub_and_clears_source_caches(leased_event):
    evt, _, _ = leased_event
    model = Mock()
    evt.ctx.call_llm = model
    evt.ctx.__dict__.update(event_count=999, current_turn_event_count=88, turn="old", prior="old", transcript_ref="old")
    evt.ctx.signal_evidence[(5, "any")] = ("old",)
    fork = evt.ctx.fork(evt.ctx.transcript.fork())
    assert fork.call_llm is model
    assert fork.signal_evidence == {}
    assert fork.prepared_evidence is None
    assert "event_count" not in fork.__dict__
    assert "current_turn_event_count" not in fork.__dict__
    assert "turn" not in fork.__dict__
    assert "prior" not in fork.__dict__
    assert "transcript_ref" not in fork.__dict__
    fork.transcript.release()
    evt.ctx.transcript.release()


def test_fail_closed_handler_evidence_failure_blocks(leased_event):
    evt, _, _ = leased_event

    @on(Event.PostToolUse, on_incomplete="Ask the user for this write again.")
    def guard(evt):
        assert len(evt.ctx.t) == 42
        raise EvidenceIncomplete("output_limit", "projection text exceeds output budget")

    overrides = reqenv.RequestOverrides(env={}, cwd="/tmp", client_ppid=1, session_id="s")
    with reqenv.use_request(overrides):
        envelope = dispatch(Event.PostToolUse, evt)
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "guard could not judge this call (output_limit: projection text exceeds output budget)" in reason
    assert reason.endswith("Ask the user for this write again.")
    assert overrides.evidence_gaps == []
    assert evt.ctx.transcript.pins.pending == 0


def test_fail_closed_condition_evidence_failure_still_runs_the_hook(leased_event):
    evt, _, _ = leased_event

    class Incomplete(CustomCondition):
        def check(self, evt):
            raise EvidenceIncomplete("entry_limit", "condition incomplete")

    @on(Event.PostToolUse, only_if=[Incomplete()], on_incomplete="Ask again.")
    def guard(evt):
        return evt.block("judged")

    overrides = reqenv.RequestOverrides(env={}, cwd="/tmp", client_ppid=1, session_id="s")
    with reqenv.use_request(overrides):
        envelope = dispatch(Event.PostToolUse, evt)
    assert envelope["hookSpecificOutput"]["permissionDecisionReason"] == "judged"
    assert overrides.evidence_gaps == []
    assert evt.ctx.transcript.pins.pending == 0


def test_fail_closed_hook_without_a_verdict_blocks_the_fold():
    from concurrent.futures import Future

    from captain_hook.dispatch import combine
    from captain_hook.types import HookSpec, RegisteredHook

    closed = RegisteredHook(spec=HookSpec(events=Event.PreToolUse, on_incomplete="Ask again."), name="guard")
    skipped = RegisteredHook(spec=HookSpec(events=Event.PreToolUse), name="advice")
    never_started: Future = Future()
    never_started.cancel()
    also_cancelled: Future = Future()
    also_cancelled.cancel()
    envelope = combine(Event.PreToolUse, [skipped, closed], [also_cancelled, never_started], 0.0)
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert envelope["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        "guard could not judge this call (deadline:"
    )
    assert combine(Event.PreToolUse, [skipped], [also_cancelled], 0.0) is None


def test_fail_closed_rejects_async_hooks():
    from captain_hook.app import AsyncDecisionError

    with pytest.raises(AsyncDecisionError, match="on_incomplete"):
        on(Event.Notification, async_=True, on_incomplete="Ask again.")


def test_fail_closed_hook_blocks_a_dispatch_inside_the_deadline_margin(leased_event):
    evt, loaded, _ = leased_event

    @on(Event.PostToolUse)
    def advice(evt):
        raise AssertionError("advisory hooks stay unrun inside the margin")

    @on(Event.PostToolUse, on_incomplete="Ask again.")
    def guard(evt):
        raise AssertionError("a fail-closed hook has no time to judge inside the margin")

    envelope = dispatch(Event.PostToolUse, evt, advisory=False)
    assert envelope["hookSpecificOutput"]["permissionDecisionReason"].startswith("guard could not judge this call")
    assert loaded == []
    assert evt.ctx.transcript.pins.pending == 0
