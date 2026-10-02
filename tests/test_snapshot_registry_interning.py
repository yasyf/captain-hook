import threading
from types import SimpleNamespace

import pytest

from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.snapshots.worker import MAX_REGISTRY_GENERATIONS, Owner, failure, registry_key
from captain_hook.util.caching import LRUDict
from tests.test_snapshot_owner import context, request

SPECS = [{"name": "custom_edit", "behaves_like": "Edit", "span_edit": None}]


def fake_owner(register, native):
    owner = object.__new__(Owner)
    owner.store = SimpleNamespace(register_tool_registry=register, request=native)
    owner.incomplete_type = type("SnapshotIncomplete", (Exception,), {})
    owner.registry_generations = LRUDict(MAX_REGISTRY_GENERATIONS)
    owner.registry_guard = threading.Lock()
    return owner


def test_owner_registers_each_distinct_registry_key_once():
    registrations = []
    seen = []

    def register(registry, *, context):
        registrations.append(registry)
        return f"generation-{len(registrations)}"

    def native(body, *, context, cancellation):
        seen.append(context["registry_generation"])
        return failure(body["id"], "missing", "fixture")

    owner = fake_owner(register, native)
    body = request()["snapshot"]["request"]
    owner.call(body, context(), object(), SPECS)
    owner.call(body, context(), object(), [dict(reversed(SPECS[0].items()))])
    owner.call(body, context() | {"claimant": "other"}, object(), SPECS)
    assert registrations == [SPECS]
    owner.call(body, context(admission="review"), object(), SPECS)
    owner.call(body, context() | {"authority": {"kind": "user", "effective_uid": "502"}}, object(), SPECS)
    owner.call(body, context(), object(), [{**SPECS[0], "behaves_like": "Read"}])
    assert len(registrations) == 4
    assert seen == ["generation-1", "generation-1", "generation-1", "generation-2", "generation-3", "generation-4"]


def test_owner_reuses_reordered_registry_definitions():
    registrations = []

    def register(registry, *, context):
        registrations.append(registry)
        return "generation"

    owner = fake_owner(register, None)
    specs = [*SPECS, {"name": "custom_read", "behaves_like": "Read", "span_edit": None}]

    assert owner.registry_generation(specs, context()) == "generation"
    assert owner.registry_generation(list(reversed(specs)), context()) == "generation"
    assert registrations == [specs]
    assert len(owner.registry_generations) == 1


@pytest.mark.parametrize(
    "specs",
    [
        pytest.param([*SPECS, *SPECS], id="duplicate"),
        pytest.param([{"name": "custom_edit", "behaves_like": "Edit"}], id="missing-field"),
        pytest.param([{**SPECS[0], "unexpected": True}], id="unknown-field"),
        pytest.param([{**SPECS[0], "name": ""}], id="empty-name"),
    ],
)
def test_owner_passes_invalid_definitions_to_native_without_caching(specs):
    registrations = []

    def register(registry, *, context):
        registrations.append(registry)
        if registry != SPECS:
            raise EvidenceIncomplete("invalid_request", "invalid registry")
        return "generation"

    owner = fake_owner(register, None)
    body = request()["snapshot"]["request"]
    assert owner.registry_generation(SPECS, context()) == "generation"

    for _ in range(2):
        assert owner.call(body, context(), object(), specs)["status"] == "invalid_request"

    assert registrations == [SPECS, specs, specs]
    assert len(owner.registry_generations) == 1


def test_owner_registry_memo_bounds_authority_variants_and_refreshes_hits():
    registrations = []

    def register(registry, *, context):
        registrations.append(context)
        return "generation"

    owner = fake_owner(register, None)
    scopes = [
        context() | {"authority": {"kind": "restricted_roots", "effective_uid": "501", "roots": [f"/fixture/root-{i}"]}}
        for i in range(MAX_REGISTRY_GENERATIONS + 1)
    ]
    for scope in scopes[:-1]:
        assert owner.registry_generation(SPECS, scope) == "generation"
    assert owner.registry_generation(SPECS, scopes[0]) == "generation"
    assert owner.registry_generation(SPECS, scopes[-1]) == "generation"

    assert len(owner.registry_generations) == MAX_REGISTRY_GENERATIONS
    assert registry_key(SPECS, scopes[0]) in owner.registry_generations
    assert registry_key(SPECS, scopes[1]) not in owner.registry_generations
    assert len(registrations) == MAX_REGISTRY_GENERATIONS + 1
    assert owner.registry_generation(SPECS, scopes[1]) == "generation"
    assert len(registrations) == MAX_REGISTRY_GENERATIONS + 2
    assert len(owner.registry_generations) == MAX_REGISTRY_GENERATIONS


def test_fixture_owner_rejects_duplicates_after_valid_registration():
    from captain_hook.testing.snapshots import FixtureOwner

    fixture = FixtureOwner()
    try:
        fixture.owner.registry_generation(SPECS, fixture.context)
        for _ in range(2):
            with pytest.raises(fixture.owner.incomplete_type, match="duplicate"):
                fixture.owner.registry_generation([*SPECS, *SPECS], fixture.context)
        assert len(fixture.owner.registry_generations) == 1
    finally:
        fixture.close()


def test_fixture_owner_registers_an_unchanged_registry_once(monkeypatch):
    from captain_hook.testing.snapshots import FixtureOwner

    fixture = FixtureOwner()
    try:
        fixture.client.bind_tool_registry({"mcp_edit": ("Edit", {"path": "file", "content": "replacement"})})
        assert fixture.owner.registry_generations.maxsize == MAX_REGISTRY_GENERATIONS
        registrations = []
        native = fixture.owner.store.register_tool_registry

        def counting(specs, *, context):
            registrations.append(specs)
            return native(specs, context=context)

        monkeypatch.setattr(fixture.owner.store, "register_tool_registry", counting)
        for _ in range(3):
            assert fixture.client.call("stats")["status"] == "ok"
        assert registrations == [fixture.client.tool_registry()]
        generation = native(fixture.client.tool_registry(), context=fixture.context)
        assert list(fixture.owner.registry_generations.values()) == [generation]
    finally:
        fixture.close()
