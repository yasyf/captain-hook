from dataclasses import replace
from pathlib import Path

import pytest

from captain_hook import context, signals
from captain_hook.context import HookContext
from captain_hook.events import PostToolUseEvent
from captain_hook.primitives.llm import GateVerdict, consume_signals, llm_evaluate
from captain_hook.session import SessionStore
from captain_hook.state import PrimitiveState, record_fire, text_hash
from captain_hook.types import Signal, Signals


class SnapshotFixture:
    def __init__(self):
        self.released = False
        self.signal_reads = 0
        self.render_reads = 0
        self.length_reads = 0
        self.path = Path('/fixture/session.jsonl')
        self.evidence_ref = 'owner:snapshot:generation'

    def __len__(self):
        assert not self.released
        self.length_reads += 1
        return 7

    @property
    def current_turn(self):
        assert not self.released
        return self

    def signal_texts(self, *, window, origin):
        assert not self.released
        self.signal_reads += 1
        return ['trigger {"key": "value"}']

    def render(self, **kwargs):
        assert not self.released
        self.render_reads += 1
        return 'frozen {"key": "value"}'

    def release(self):
        self.released = True


@pytest.fixture
def snapshot_event(tmp_path, monkeypatch):
    monkeypatch.setattr('captain_hook.snapshots.client.RemoteSession', SnapshotFixture)
    monkeypatch.setattr(context, 'RemoteSession', SnapshotFixture)
    monkeypatch.setattr(signals, 'RemoteSession', SnapshotFixture)
    snapshot = SnapshotFixture()
    ctx = HookContext(session=SessionStore(tmp_path), transcript=snapshot, settings=None)
    return PostToolUseEvent(_raw={'tool_name': 'Bash'}, ctx=ctx), snapshot


@pytest.mark.parametrize('lazy', [False, True])
def test_model_retries_and_bookkeeping_use_prepared_generation(snapshot_event, monkeypatch, lazy):
    evt, snapshot = snapshot_event
    if lazy:
        from captain_hook.transcripts import lazy_transcript

        evt.ctx.transcript = lazy_transcript('/fixture/session.jsonl', loader=lambda _: snapshot)
    prompts = []

    def model(prompt, response_model, **kwargs):
        assert snapshot.released
        prompts.append(prompt)
        if len(prompts) == 1:
            raise RuntimeError('retry this model failure')
        return GateVerdict(block=True, reasoning='confirmed')

    monkeypatch.setattr('spawnllm.extract_sync', model)
    sig = Signals(patterns=[Signal(pattern='trigger')], threshold=1)
    verdict = llm_evaluate(evt, 'judge', GateVerdict, hook='fixture', signals=sig, transcript='full')
    assert verdict.block
    assert prompts[0] == prompts[1]
    assert 'frozen {"key": "value"}' in prompts[0]
    assert 'path=' not in prompts[0]
    assert 'evidence="owner:snapshot:generation"' in prompts[0]
    assert snapshot.render_reads == snapshot.signal_reads == 1
    assert consume_signals(evt, sig, 'fixture') == ['trigger {"key": "value"}']
    record_fire(evt)
    state = evt.ctx.s[PrimitiveState].get()
    assert state.last_fired_at == 7
    assert state.consumed == {'fixture': {text_hash('trigger {"key": "value"}')}}
    assert snapshot.length_reads == 2
    assert evt.ctx.prepared_evidence.prompt == prompts[-1]


def test_context_forks_have_independent_signal_evidence(snapshot_event):
    evt, snapshot = snapshot_event
    signals.transcript_texts(evt, 5)
    fork = replace(evt.ctx, transcript=SnapshotFixture())
    assert fork.signal_evidence == {}
    assert fork.prepared_evidence is None
    assert len(evt.ctx.signal_evidence) == 1


def test_postmodel_consumption_rechecks_mutable_state(snapshot_event, monkeypatch):
    evt, snapshot = snapshot_event
    sig = Signals(patterns=[Signal(pattern='trigger')], threshold=1)

    def model(*args, **kwargs):
        assert snapshot.released
        with evt.ctx.s[PrimitiveState].mutate() as state:
            state.consumed['fixture'] = {text_hash('trigger {"key": "value"}')}
        return GateVerdict(block=True, reasoning='confirmed')

    monkeypatch.setattr('spawnllm.extract_sync', model)
    assert llm_evaluate(evt, 'judge', GateVerdict, hook='fixture', signals=sig).block
    assert consume_signals(evt, sig, 'fixture') is None
    assert snapshot.signal_reads == 1


def test_evidence_incomplete_is_not_retried_or_swallowed(snapshot_event, monkeypatch):
    from captain_hook.snapshots.client import EvidenceIncomplete

    evt, snapshot = snapshot_event
    calls = []

    def model(*args, **kwargs):
        calls.append(True)
        raise EvidenceIncomplete('deadline', 'projection incomplete')

    monkeypatch.setattr('spawnllm.extract_sync', model)
    with pytest.raises(EvidenceIncomplete, match='projection incomplete'):
        llm_evaluate(evt, 'judge', GateVerdict, hook='fixture')
    assert calls == [True]


def test_primitive_propagates_incomplete_evidence(snapshot_event, monkeypatch):
    from captain_hook.app import _state
    from captain_hook.primitives import llm
    from captain_hook.snapshots.client import EvidenceIncomplete
    from captain_hook.types import Event

    evt, snapshot = snapshot_event

    def incomplete(*args, **kwargs):
        raise EvidenceIncomplete('source_limit', 'source exceeds policy')

    monkeypatch.setattr(llm, 'llm_evaluate', incomplete)
    llm.llm_gate('judge', message='blocked', events=Event.PostToolUse)
    spec = _state.hooks[-1]
    with pytest.raises(EvidenceIncomplete, match='source exceeds policy'):
        spec.handler(evt)


def test_prompt_check_propagates_incomplete_evidence(snapshot_event, monkeypatch):
    from captain_hook.primitives.llm import prompt_check
    from captain_hook.snapshots.client import EvidenceIncomplete

    evt, snapshot = snapshot_event

    def incomplete(*args, **kwargs):
        raise EvidenceIncomplete('output_limit', 'render exceeds policy')

    monkeypatch.setattr(evt.ctx, 'call_llm', incomplete)
    with pytest.raises(EvidenceIncomplete, match='render exceeds policy'):
        prompt_check(evt, 'judge', prefix='fixture', include_reasoning=False)


def test_waiting_uses_snapshot_and_preserves_payload_shortcut(snapshot_event, monkeypatch):
    from captain_hook.conditions import is_waiting
    from captain_hook.events import StopEvent
    from captain_hook.snapshots.client import EvidenceIncomplete

    evt, snapshot = snapshot_event
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        raise EvidenceIncomplete('deadline', 'probe incomplete')

    snapshot.activity_probe = probe
    with pytest.raises(EvidenceIncomplete, match='probe incomplete'):
        is_waiting(StopEvent(_raw={}, ctx=evt.ctx))
    assert len(calls) == 1
    assert {'AskUserQuestion', 'ExitPlanMode'} <= set(calls[0]['human_facing_tools'])
    assert is_waiting(StopEvent(_raw={'session_crons': [{'id': 'cron'}]}, ctx=evt.ctx))
    assert len(calls) == 1


@pytest.mark.parametrize('prompts,last,expected', [
    (['first'], 4, '[first]\nfirst'),
    (['first', 'second'], 4, '[first]\nfirst\n\n[recent -1]\nsecond'),
    (['first', 'middle', 'first'], 1, '[first]\nfirst\n\n[recent -1]\nfirst'),
    (['first', 'second'], 0, '[first]\nfirst'),
])
def test_user_messages_preserves_order_and_duplicate_text(snapshot_event, monkeypatch, prompts, last, expected):
    from captain_hook.contexts import UserMessages
    from captain_hook.snapshots import client

    evt, snapshot = snapshot_event
    monkeypatch.setattr(client, 'RemoteSession', SnapshotFixture)
    snapshot.prompts = lambda *, selection, count: prompts[:count] if selection == 'first' else prompts[-count:]
    assert UserMessages(last=last).content(evt) == expected


def test_unprepared_postmodel_signal_window_fails_without_reopening(snapshot_event, monkeypatch):
    from captain_hook.snapshots.client import EvidenceIncomplete

    evt, snapshot = snapshot_event
    monkeypatch.setattr('spawnllm.extract_sync', lambda *args, **kwargs: GateVerdict(block=False, reasoning='ok'))
    llm_evaluate(evt, 'judge', GateVerdict, hook='fixture')
    assert evt.ctx.prepared_evidence.source_ref == 'owner:snapshot:generation'
    with pytest.raises(EvidenceIncomplete, match='not prepared'):
        signals.transcript_texts(evt, 300)
    assert snapshot.signal_reads == 1
