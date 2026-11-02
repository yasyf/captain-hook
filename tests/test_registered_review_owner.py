from unittest.mock import Mock

from captain_hook.snapshots.client import HOST_SCHEMA
from captain_hook.snapshots.review import REVIEW_POLICY
from captain_hook.snapshots.worker import Owner, empty_usage


def owner(store):
    value = Owner.__new__(Owner)
    value.store = store
    value.review_inputs = {}
    value.registry_generation = Mock(return_value="registry")
    value.incomplete_type = type("Incomplete", (Exception,), {})
    value._run_policy = Mock(return_value={"kind": "review", "source_revision": "revision", "candidates_json": []})
    return value


def request():
    return {
        "schema": HOST_SCHEMA,
        "id": "registered",
        "operation": "prepare_review",
        "policy": REVIEW_POLICY,
        "path": "/source.jsonl",
    }


def test_policy_publishes_inside_native_scope_without_raw_acquisition():
    events = []
    frame = Mock(usage=empty_usage(), work={})
    frame.publish_projection.side_effect = lambda request, **arguments: (
        events.append("publish") or arguments | {"complete": True, "cursor": None}
    )
    store = Mock()

    def consume(path, **arguments):
        events.append("open")
        result = arguments["handler"](frame)
        events.append("closed")
        return result

    store.run_registered_review.side_effect = consume
    result = owner(store).call(request(), {}, object(), [])
    assert events == ["open", "publish", "closed"]
    assert result["status"] == "ok"
    assert result["data"]["source_revision"] == "revision"
    store.borrow_snapshot.assert_not_called()
    store.run_registered_review.assert_called_once()


def test_unfinished_source_fails_open_once_without_policy_or_retry():
    store = Mock()
    store.run_registered_review.return_value = {"kind": "review_preparation", "usage": empty_usage()}
    evaluator = owner(store)
    result = evaluator.call(request(), {}, object(), [])
    assert result["status"] == "deadline"
    assert not result["complete"]
    store.run_registered_review.assert_called_once()
    store.borrow_snapshot.assert_not_called()
    evaluator._run_policy.assert_not_called()
