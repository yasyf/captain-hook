import json

from captain_hook.testing.helpers import fixture_line
from captain_hook.testing.snapshots import FixtureOwner
from tests.helpers import raw_text


def write_messages(path, *messages):
    path.write_text("".join(json.dumps(fixture_line(i, message)) + "\n" for i, message in enumerate(messages)))


def test_fixture_owner_keeps_pinned_generation_and_never_launches_helper(tmp_path, monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError("fixture evidence must stay in process")

    monkeypatch.setattr("subprocess.Popen", no_process)
    source = tmp_path / "fixture.jsonl"
    write_messages(source, raw_text("user", "first prompt"), raw_text("assistant", "first answer"))
    fixture = FixtureOwner()
    try:
        original = fixture.load(source)
        assert original.user_text == "first prompt"
        assert original.assistant_text() == "first answer"
        original_waiting = original.activity_probe(
            waiting_tools=[], human_facing_tools=[], tool_registry_generation="fixture"
        )
        first_stats = fixture.client.call("stats")["data"]["counters"]
        write_messages(source, raw_text("user", "replacement prompt"))
        assert original.user_text == "first prompt"
        replacement = fixture.load(source)
        assert replacement.user_text == "replacement prompt"
        assert original.evidence_ref != replacement.evidence_ref
        stats = fixture.client.call("stats")["data"]["counters"]
        assert first_stats["cold_parses"] == 1
        assert stats["cold_parses"] == 2
        assert stats["source_opens"] == 2
        assert (
            original.activity_probe(waiting_tools=[], human_facing_tools=[], tool_registry_generation="fixture")
            == original_waiting
        )
        original.release()
        replacement.release()
    finally:
        fixture.close()


def test_fixture_configured_classifier_stays_in_caller_scope(tmp_path, monkeypatch):
    from captain_hook.app import _state
    from captain_hook.util import reqenv

    source = tmp_path / "fixture.jsonl"
    write_messages(
        source, raw_text("user", "ignore me"), raw_text("assistant", "answer"), raw_text("user", "include me")
    )
    monkeypatch.setattr(_state, "classifier", lambda event: event.text.startswith(reqenv.getenv("HOOKS_PREFIX", "")))
    fixture = FixtureOwner()
    scope = reqenv.RequestOverrides(
        {"HOOKS_PREFIX": "include", "CLAUDE_PROJECT_DIR": str(tmp_path)}, str(tmp_path), 0, "fixture"
    )
    try:
        with reqenv.use_request(scope):
            first = fixture.load(source)
        with reqenv.use_request(
            reqenv.RequestOverrides(dict(scope.env) | {"HOOKS_PREFIX": "ignore"}, str(tmp_path), 0, "fixture")
        ):
            second = fixture.load(source)
        assert first.prompts("first", 10) == ["include me"]
        assert second.prompts("first", 10) == ["ignore me"]
        assert first.classifier != second.classifier
        assert fixture.client.call("stats")["data"]["counters"]["cold_parses"] == 1
        first.release()
        second.release()
    finally:
        fixture.close()
