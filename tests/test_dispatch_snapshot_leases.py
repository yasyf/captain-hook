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


class LeasedFixture:
    def __init__(self, released, name='source'):
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
    monkeypatch.setattr('captain_hook.snapshots.client.RemoteSession', LeasedFixture)

    def load(path):
        loaded.append(path)
        return LeasedFixture(released)

    source = lazy_transcript('/fixture.jsonl', loader=load)
    evt = PostToolUseEvent(_raw={'tool_name': 'Bash'}, ctx=HookContext(SessionStore(tmp_path), source, None))
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
    assert loaded == ['/fixture.jsonl']
    assert sorted(released) == ['1', '2', 'source']
    assert evt.ctx.transcript.pins.pending == 0


def test_condition_failure_releases_every_preparation_branch(leased_event):
    evt, loaded, released = leased_event

    class Incomplete(CustomCondition):
        def check(self, evt):
            assert len(evt.ctx.t) == 42
            raise EvidenceIncomplete('entry_limit', 'condition incomplete')

    @on(Event.PostToolUse, only_if=[Incomplete()])
    def first(evt):
        raise AssertionError('handler must not run')

    @on(Event.PostToolUse)
    def second(evt):
        raise AssertionError('handler must not run')

    with pytest.raises(EvidenceIncomplete, match='condition incomplete'):
        dispatch(Event.PostToolUse, evt)
    assert sorted(released) == ['1', 'source']
    assert evt.ctx.transcript.pins.pending == 0


def test_handler_incomplete_propagates_and_releases(leased_event):
    evt, loaded, released = leased_event

    @on(Event.PostToolUse)
    def first(evt):
        assert len(evt.ctx.t) == 42
        raise EvidenceIncomplete('output_limit', 'handler incomplete')

    with pytest.raises(EvidenceIncomplete, match='handler incomplete'):
        dispatch(Event.PostToolUse, evt)
    assert sorted(released) == ['1', 'source']
    assert evt.ctx.transcript.pins.pending == 0


def test_context_fork_preserves_model_stub_and_clears_source_caches(leased_event):
    evt, _, _ = leased_event
    model = Mock()
    evt.ctx.call_llm = model
    evt.ctx.__dict__.update(event_count=999, current_turn_event_count=88, turn='old', prior='old', transcript_ref='old')
    evt.ctx.signal_evidence[(5, 'any')] = ('old',)
    fork = evt.ctx.fork(evt.ctx.transcript.fork())
    assert fork.call_llm is model
    assert fork.signal_evidence == {}
    assert fork.prepared_evidence is None
    assert 'event_count' not in fork.__dict__
    assert 'current_turn_event_count' not in fork.__dict__
    assert 'turn' not in fork.__dict__
    assert 'prior' not in fork.__dict__
    assert 'transcript_ref' not in fork.__dict__
    fork.transcript.release()
    evt.ctx.transcript.release()
