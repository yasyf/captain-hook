from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cc_transcript.ids import SessionId
from loguru import logger

from captain_hook import transcripts
from captain_hook.session import SessionSlot, ensure_session
from captain_hook.snapshots.client import GraphSources
from captain_hook.state import RegisteredTranscript, RegisteredTranscripts


@pytest.fixture(autouse=True)
def clear_registered_sources():
    cache = getattr(transcripts, "REGISTERED_SOURCES", None)
    if cache is not None:
        cache.cache_clear()
    yield
    if cache is not None:
        cache.cache_clear()


def registration(session_dir: Path, *ids: str) -> Path:
    slot = SessionSlot(session_dir, RegisteredTranscripts)
    slot.set(RegisteredTranscripts(entries=[
        RegisteredTranscript(provider="codex", thread_id=thread_id, registered_at=0) for thread_id in ids
    ]))
    assert slot.path is not None
    return slot.path


@pytest.fixture
def reads(monkeypatch):
    observed: list[Path] = []
    read_text = Path.read_text

    def record(path, *args, **kwargs):
        if path.name == "registered_transcripts.json":
            observed.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record)
    return observed


def test_unchanged_large_registration_reads_and_validates_once(tmp_path, monkeypatch, reads):
    ids = tuple(f"thread-{index:04}" for index in range(5523))
    path = registration(tmp_path, *ids)
    validated: list[int] = []
    validate = RegisteredTranscripts.model_validate_json

    def count(cls, text, **kwargs):
        validated.append(len(text))
        return validate(text, **kwargs)

    monkeypatch.setattr(RegisteredTranscripts, "model_validate_json", classmethod(count))
    first = transcripts.registered_sources(tmp_path)
    values = [transcripts.registered_sources(tmp_path) for _ in range(100)]
    print(f"registered_sources entries={len(ids)} calls=101 reads={len(reads)} validations={len(validated)}")
    assert all(value is first for value in values)
    assert first.thread_ids == ids
    assert first.direct_paths == ()
    assert len(first.roots) == 1
    assert reads == [path]
    assert len(validated) == 1


def test_cached_sources_preserve_deduplication_and_registration_order(tmp_path):
    slot = SessionSlot(tmp_path, RegisteredTranscripts)
    direct = tmp_path / "direct.jsonl"
    slot.set(RegisteredTranscripts(entries=[
        RegisteredTranscript(provider="codex", thread_id="second"),
        RegisteredTranscript(provider="codex", path=str(direct)),
        RegisteredTranscript(provider="codex", thread_id="first"),
        RegisteredTranscript(provider="codex", thread_id="second"),
        RegisteredTranscript(provider="other", path=str(direct)),
    ]))
    first = transcripts.registered_sources(tmp_path)
    assert first.thread_ids == ("second", "first")
    assert first.direct_paths == (direct,)
    assert transcripts.registered_sources(tmp_path) is first


def test_new_registration_is_visible_on_the_next_call(reads):
    session_id = SessionId("cache-new-registration")
    session_dir = ensure_session(session_id)
    registration(session_dir, "first")
    assert transcripts.registered_sources(session_dir).thread_ids == ("first",)
    transcripts.register_transcript(session_id, thread_id="second")
    before = len(reads)
    fresh = transcripts.registered_sources(session_dir)
    assert fresh.thread_ids == ("first", "second")
    assert transcripts.registered_sources(session_dir) is fresh
    assert len(reads) == before + 1


@pytest.mark.parametrize("replace", [False, True])
def test_same_size_changed_registration_invalidates_even_with_restored_mtime(tmp_path, reads, replace):
    path = registration(tmp_path, "old")
    before = path.stat()
    assert transcripts.registered_sources(tmp_path).thread_ids == ("old",)
    text = path.read_text().replace('"old"', '"new"')
    target = path.with_suffix(".replacement") if replace else path
    target.write_text(text)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if replace:
        target.replace(path)
    after = path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert (after.st_ino, after.st_ctime_ns) != (before.st_ino, before.st_ctime_ns)
    count = len(reads)
    fresh = transcripts.registered_sources(tmp_path)
    assert fresh.thread_ids == ("new",)
    assert transcripts.registered_sources(tmp_path) is fresh
    assert len(reads) == count + 1


def test_delete_and_recreate_do_not_keep_a_negative_result(tmp_path, reads):
    path = registration(tmp_path, "old")
    transcripts.registered_sources(tmp_path)
    path.unlink()
    before = len(reads)
    assert transcripts.registered_sources(tmp_path) == GraphSources()
    assert transcripts.registered_sources(tmp_path) == GraphSources()
    assert len(reads) == before
    registration(tmp_path, "new")
    fresh = transcripts.registered_sources(tmp_path)
    assert fresh.thread_ids == ("new",)
    assert transcripts.registered_sources(tmp_path) is fresh
    assert len(reads) == before + 1


@pytest.mark.parametrize("text", ["{", json.dumps({"entries": [{"provider": "codex"}]})])
def test_invalid_registration_warns_on_each_read_and_recovers(tmp_path, reads, text):
    path = registration(tmp_path, "old")
    transcripts.registered_sources(tmp_path)
    path.write_text(text)
    warnings = []
    sink = logger.add(lambda message: warnings.append(message.record["message"]))
    try:
        before = len(reads)
        assert transcripts.registered_sources(tmp_path) == GraphSources()
        assert transcripts.registered_sources(tmp_path) == GraphSources()
        assert len(reads) == before + 2
        assert warnings.count("failed to read session state") == 2
    finally:
        logger.remove(sink)
    registration(tmp_path, "fixed")
    assert transcripts.registered_sources(tmp_path).thread_ids == ("fixed",)


def test_successful_empty_registration_is_cacheable(tmp_path, reads):
    path = registration(tmp_path)
    first = transcripts.registered_sources(tmp_path)
    assert first == GraphSources()
    assert transcripts.registered_sources(tmp_path) is first
    assert reads == [path]


def test_read_permission_failures_are_not_cached(tmp_path, monkeypatch):
    path = registration(tmp_path, "old")
    read_text = Path.read_text
    attempts = []
    warnings = []

    def denied(selected, *args, **kwargs):
        if selected == path:
            attempts.append(selected)
            raise PermissionError("registration denied")
        return read_text(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    sink = logger.add(lambda message: warnings.append(message.record["message"]))
    try:
        assert transcripts.registered_sources(tmp_path) == GraphSources()
        assert transcripts.registered_sources(tmp_path) == GraphSources()
        assert attempts == [path, path]
        assert warnings.count("failed to read session state") == 2
    finally:
        logger.remove(sink)
    monkeypatch.setattr(Path, "read_text", read_text)
    assert transcripts.registered_sources(tmp_path).thread_ids == ("old",)


def test_stat_failure_preserves_the_existing_slot_read_behavior(tmp_path, monkeypatch):
    path = registration(tmp_path, "old")
    stat = Path.stat

    def denied(selected, *args, **kwargs):
        if selected == path:
            raise PermissionError("stat denied")
        return stat(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    try:
        loaded = SessionSlot(tmp_path, RegisteredTranscripts).get()
    except OSError as error:
        with pytest.raises(type(error)):
            transcripts.registered_sources(tmp_path)
    else:
        assert loaded is None
        assert transcripts.registered_sources(tmp_path) == GraphSources()


def test_postread_stat_failure_does_not_replace_success_with_an_error(tmp_path, monkeypatch, reads):
    registration(tmp_path, "old")
    stamp = transcripts.registered_sources_stamp
    checks = []

    def lose_postread(path):
        checks.append(path)
        return stamp(path) if len(checks) == 1 else None

    monkeypatch.setattr(transcripts, "registered_sources_stamp", lose_postread)
    first = transcripts.registered_sources(tmp_path)
    assert first.thread_ids == ("old",)
    monkeypatch.setattr(transcripts, "registered_sources_stamp", stamp)
    fresh = transcripts.registered_sources(tmp_path)
    assert fresh == first
    assert fresh is not first
    assert len(reads) == 2


def test_replacement_during_read_is_not_published_under_the_old_stamp(tmp_path, monkeypatch):
    path = registration(tmp_path, "old")
    read_text = Path.read_text
    reads = []

    def replace_after_read(selected, *args, **kwargs):
        text = read_text(selected, *args, **kwargs)
        if selected == path:
            reads.append(selected)
            if len(reads) == 1:
                replacement = path.with_suffix(".replacement")
                replacement.write_text(text.replace('"old"', '"new"'))
                replacement.replace(path)
        return text

    monkeypatch.setattr(Path, "read_text", replace_after_read)
    old = transcripts.registered_sources(tmp_path)
    assert old.thread_ids == ("old",)
    fresh = transcripts.registered_sources(tmp_path)
    assert fresh.thread_ids == ("new",)
    assert transcripts.registered_sources(tmp_path) is fresh
    assert len(reads) == 2


def test_parallel_unchanged_readers_share_one_real_registration_read(tmp_path, reads):
    path = registration(tmp_path, *(f"thread-{index}" for index in range(128)))
    start = threading.Barrier(8, timeout=5)

    def read():
        start.wait()
        return transcripts.registered_sources(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read) for _ in range(8)]
        values = [future.result(timeout=10) for future in futures]
    assert all(value is values[0] for value in values)
    assert reads == [path]


def test_only_one_successful_session_roster_is_retained(tmp_path, reads):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_path = registration(first, "first")
    second_path = registration(second, "second")
    transcripts.registered_sources(first)
    transcripts.registered_sources(second)
    assert transcripts.registered_sources(first).thread_ids == ("first",)
    assert reads == [first_path, second_path, first_path]


def test_sessions_root_change_invalidates_the_cached_graph_scope(tmp_path, monkeypatch, reads):
    from cc_transcript import codex

    path = registration(tmp_path, "thread")
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    monkeypatch.setattr(codex, "sessions_root", lambda: first_root)
    first = transcripts.registered_sources(tmp_path)
    assert first.roots == (first_root,)
    assert transcripts.registered_sources(tmp_path) is first
    monkeypatch.setattr(codex, "sessions_root", lambda: second_root)
    second = transcripts.registered_sources(tmp_path)
    assert second.roots == (second_root,)
    assert second.thread_ids == first.thread_ids == ("thread",)
    assert second is not first
    assert transcripts.registered_sources(tmp_path) is second
    monkeypatch.setattr(codex, "sessions_root", lambda: first_root)
    returned = transcripts.registered_sources(tmp_path)
    assert returned.roots == first.roots == (first_root,)
    assert returned is not first
    assert reads == [path, path, path]
