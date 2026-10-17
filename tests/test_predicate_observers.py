import json
from itertools import count
from pathlib import Path

import pytest

from captain_hook.app import hook as register_hook
from captain_hook.conditions import TRANSCRIPT_SELECTORS, TURN_SELECTORS, check_condition, planned_predicates
from captain_hook.snapshots.client import (
    CORE_SCHEMA,
    DEFAULT_LIMITS,
    HOST_SCHEMA,
    OBSERVABLE_KINDS,
    OBSERVER_BYTES,
    OBSERVER_LIMIT,
    OBSERVER_PLAN,
    EvidenceIncomplete,
    GraphSources,
    Lease,
    PreparedGraphEvidence,
    RemoteSession,
    SnapshotClient,
    SnapshotProtocolError,
    canonical_json,
    list_predicate,
    observing,
    tool_predicate,
)
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.types import (
    And,
    CustomCondition,
    Event,
    HookSpec,
    LambdaCondition,
    Not,
    Or,
    RanCommand,
    ReadFile,
    Regex,
    Tool,
    TouchedFile,
    UsedSkill,
    UsedTool,
)
from tests.helpers import build_ctx, make_pre_tool_event, raw_assistant, raw_text, raw_tool_use
from tests.test_snapshot_client import description, response

TRANSCRIPT = "[]"
TURN = '[{"kind":"current_turn"}]'
READ = {"kind": "has_read", "pattern": "README.md", "subagents": True}
EDIT = {"kind": "has_tool", "pattern": "Edit", "subagents": True}
SKILL = {"kind": "has_skill_suffix", "values": ["codex"], "subagents": True}
REGEX = {"kind": "has_command_regex", "pattern": "pytest", "subagents": True}
EDIT_GLOB = {"kind": "has_edit_to", "values": ["src/**"], "subagents": True}
READ_GLOB = {"kind": "has_read_glob", "values": ["*.md"], "subagents": True}
GRAPH_HANDLE = {"graph_id": "graph", "owner_epoch": "owner", "revision": "rev", "complete": True}
PLANNED = [
    pytest.param(
        UsedSkill("codex"), (TURN, '{"kind":"has_skill_suffix","subagents":true,"values":["codex"]}'), id="skill-turn"
    ),
    pytest.param(
        UsedSkill("codex", "review", scope="session"),
        (TRANSCRIPT, '{"kind":"has_skill_suffix","subagents":true,"values":["codex","review"]}'),
        id="skill-session",
    ),
    pytest.param(
        UsedTool("Edit|Write"), (TURN, '{"kind":"has_tool","pattern":"Edit|Write","subagents":true}'), id="tool-turn"
    ),
    pytest.param(
        UsedTool("Read", scope="session"),
        (TRANSCRIPT, '{"kind":"has_tool","pattern":"Read","subagents":true}'),
        id="tool-session",
    ),
    pytest.param(
        RanCommand("git", "push"),
        (TRANSCRIPT, '{"kind":"has_command","subagents":true,"values":["git","push"]}'),
        id="ran-command",
    ),
]


class ObservingClient(SnapshotClient):
    def __init__(self, *replies):
        super().__init__(lambda _: {})
        self.requests = []
        self.replies = list(replies)
        self.bind_tool_registry({})

    def call(self, operation, **arguments):
        assert operation == "release"
        return {"status": "ok"}

    def pages(self, operation, **arguments):
        self.requests.append((operation, arguments))
        if operation == "retain":
            yield {"kind": "acquired", "description": description(lease=f"retained-{len(self.requests)}")}
        elif operation == "prepare_graph":
            yield {"kind": "prepared_graph", "handle": GRAPH_HANDLE}
        elif operation == "query_graph":
            reply = self.replies.pop(0) if self.replies else {"kind": "scalar", "value": False}
            if isinstance(reply, EvidenceIncomplete):
                raise reply
            yield reply
        elif operation == "query":
            yield {"kind": "scalar", "value": True}
        else:
            raise AssertionError(operation)


class Exploding(CustomCondition):
    def check(self, evt):
        raise AssertionError("planning evaluated a custom condition")


def root_session(client):
    source = description()
    session = RemoteSession(client, Lease(client, source), Path(source["canonical_path"]), source["classifier"])
    return session.with_registered_sources(GraphSources(thread_ids=("thread-1",)))


def planned(*queries, selectors=TRANSCRIPT):
    return [(selectors, canonical_json(query)) for query in queries]


def observed(value, *observers):
    return {"kind": "observed_scalar", "value": value, "observers": list(observers)}


def graph_queries(client):
    return [arguments for operation, arguments in client.requests if operation == "query_graph"]


def sent(client):
    return [(arguments["query"], arguments.get("observers")) for arguments in graph_queries(client)]


def scalar_key(session, query):
    return (
        session.evidence_ref,
        tuple(sorted(session.classifier.items())),
        session.client,
        (session.graph, session.graph.lapses),
        canonical_json(session.selectors),
        canonical_json(query),
    )


def elsewhere(session, key):
    return {
        "evidence_ref": ("transcript:owner:snapshot:earlier", *key[1:]),
        "classifier": (key[0], (("id", "configured"), ("version", "1")), *key[2:]),
        "client": (*key[:2], ObservingClient(), *key[3:]),
        "graph": (*key[:3], (PreparedGraphEvidence(GraphSources()), 0), *key[4:]),
        "lapses": (*key[:3], (session.graph, 1), *key[4:]),
        "selectors": (*key[:4], TURN, key[5]),
    }


def reacquiring(lease):
    generations = count(1)

    def require(client=None):
        lease.handle = lease.handle | {"generation": f"reacquired-{next(generations)}"}
        lease.scalar_results.clear()
        return lease.handle.copy()

    return require


def spec(*only_if, skip_if=()):
    return HookSpec(events=Event.PreToolUse, only_if=only_if, skip_if=tuple(skip_if))


def graph_request(**fields):
    request = {
        "schema": CORE_SCHEMA,
        "id": "graph-query",
        "operation": "query_graph",
        "deadline_unix_ms": 1_790_389_614_377,
        "limits": DEFAULT_LIMITS,
        "handle": GRAPH_HANDLE,
        "selectors": [],
        "query": READ,
    }
    return {"schema": HOST_SCHEMA, "request": request | fields, "tool_registry": []}


def fixture_event():
    from captain_hook.testing.helpers import input_to_event
    from captain_hook.testing.types import Input

    return input_to_event(Event.PreToolUse, Input(command="echo fixture", tasks=[], skip_permissions=False))


def test_first_negative_demand_carries_its_siblings_and_answers_them_without_another_request():
    client = ObservingClient(observed(False, False, False))
    root = root_session(client)
    reader, editor, skilled = root.retain(), root.retain(), root.retain()

    with observing(planned(READ, EDIT, SKILL)) as plan:
        assert reader.has_read("README.md") is False
        assert editor.has_tool("Edit") is False
        assert skilled.query(SKILL) is False

    assert sent(client) == [(READ, [EDIT, SKILL])]
    assert [operation for operation, _ in client.requests].count("prepare_graph") == 1
    assert list(plan.outcomes.values()) == [False, False, False]
    assert list(editor.lease.scalar_results.values()) == [False]
    assert list(skilled.lease.scalar_results.values()) == [False]


def test_a_changed_primary_fails_unchanged_and_leaves_its_root_positive_sibling_to_ask_itself():
    changed = EvidenceIncomplete("changed", "child transcript changed during traversal")
    client = ObservingClient(changed, {"kind": "scalar", "value": True})
    root = root_session(client)
    reader, editor = root.retain(), root.retain()

    with observing(planned(READ, EDIT)) as plan:
        with pytest.raises(EvidenceIncomplete) as raised:
            reader.has_read("README.md")
        assert raised.value is changed
        assert plan.outcomes == {}
        assert reader.lease.scalar_results == {}
        assert editor.has_tool("Edit") is True

    assert sent(client) == [(READ, [EDIT]), (EDIT, [READ])]
    assert list(plan.outcomes.values()) == [True]


def test_a_root_positive_primary_reports_no_observations_and_each_sibling_asks_itself():
    client = ObservingClient({"kind": "scalar", "value": True})
    root = root_session(client)
    reader, editor, skilled = root.retain(), root.retain(), root.retain()

    with observing(planned(READ, EDIT, SKILL)) as plan:
        assert reader.has_read("README.md") is True
        assert list(plan.outcomes.values()) == [True]
        assert editor.has_tool("Edit") is False
        assert skilled.query(SKILL) is False

    assert sent(client) == [(READ, [EDIT, SKILL]), (EDIT, [SKILL]), (SKILL, None)]


def test_unknown_observations_are_asked_again_and_definite_ones_are_kept():
    client = ObservingClient(observed(False, None, True))
    root = root_session(client)
    reader, editor, skilled = root.retain(), root.retain(), root.retain()

    with observing(planned(READ, EDIT, SKILL)) as plan:
        assert reader.has_read("README.md") is False
        assert list(plan.outcomes.values()) == [True, False]
        assert skilled.query(SKILL) is True
        assert editor.has_tool("Edit") is False

    assert sent(client) == [(READ, [EDIT, SKILL]), (EDIT, None)]


@pytest.mark.parametrize(
    "reply, reason",
    [
        pytest.param(observed(False, False), "different observer count", id="observer-count"),
        pytest.param({"kind": "strings", "values": ["false"]}, "unexpected projection", id="projection"),
    ],
)
def test_an_observed_reply_outside_the_contract_is_a_protocol_error_that_records_nothing(reply, reason):
    client = ObservingClient(reply)
    session = root_session(client)

    with observing(planned(READ, EDIT, SKILL)) as plan:
        with pytest.raises(SnapshotProtocolError, match=reason):
            session.has_read("README.md")
        assert plan.outcomes == {}
        assert session.lease.scalar_results == {}


def test_an_outcome_recorded_under_the_same_scope_is_served_without_a_query():
    client = ObservingClient()
    session = root_session(client)

    with observing(planned(EDIT)) as plan:
        plan.outcomes[scalar_key(session, EDIT)] = True
        assert session.has_tool("Edit") is True

    assert sent(client) == []
    assert list(session.lease.scalar_results.values()) == [True]


@pytest.mark.parametrize("component", ["evidence_ref", "classifier", "client", "graph", "lapses", "selectors"])
def test_an_outcome_recorded_under_another_scope_is_never_served(component):
    client = ObservingClient()
    session = root_session(client)

    with observing(planned(EDIT)) as plan:
        plan.outcomes[elsewhere(session, scalar_key(session, EDIT))[component]] = True
        assert session.has_tool("Edit") is False

    assert sent(client) == [(EDIT, None)]


def test_a_lease_reacquired_at_its_own_demand_asks_under_the_new_generation(monkeypatch):
    client = ObservingClient(observed(False, False), {"kind": "scalar", "value": True})
    root = root_session(client)
    reader, editor = root.retain(), root.retain()

    with observing(planned(READ, EDIT)) as plan:
        assert reader.has_read("README.md") is False
        earlier = {scalar_key(editor, EDIT): False, scalar_key(reader, READ): False}
        assert plan.outcomes == earlier
        monkeypatch.setattr(editor.lease, "require", reacquiring(editor.lease))
        assert editor.has_tool("Edit") is True

    assert sent(client) == [(READ, [EDIT]), (EDIT, [READ])]
    assert editor.evidence_ref == "transcript:owner:snapshot:reacquired-1"
    assert plan.outcomes == earlier | {scalar_key(editor, EDIT): True}


def test_an_observation_that_outlives_its_lease_generation_is_not_recorded(monkeypatch):
    client = ObservingClient(observed(False, True))
    session = root_session(client)
    monkeypatch.setattr(session.lease, "require", reacquiring(session.lease))

    with observing(planned(READ, EDIT)) as plan:
        assert session.has_read("README.md") is False
        assert plan.outcomes == {}

    assert sent(client) == [(READ, [EDIT])]
    assert session.evidence_ref == "transcript:owner:snapshot:reacquired-2"


def test_a_lapsed_graph_drops_what_was_observed_through_it():
    bash = tool_predicate("has_tool", "Bash", subagents=True)
    client = ObservingClient(
        observed(False, False),
        EvidenceIncomplete("stale_handle", "prepared graph expired"),
        {"kind": "scalar", "value": True},
        {"kind": "scalar", "value": True},
    )
    root = root_session(client)
    reader, editor = root.retain(), root.retain()

    with observing(planned(READ, EDIT)):
        assert reader.has_read("README.md") is False
        assert reader.has_tool("Bash") is True
        assert root.graph.lapses == 1
        assert editor.has_tool("Edit") is True

    assert sent(client) == [(READ, [EDIT]), (bash, None), (bash, None), (EDIT, [READ])]
    assert [operation for operation, _ in client.requests].count("prepare_graph") == 2


def test_the_stale_handle_retry_resends_the_same_observers():
    client = ObservingClient(EvidenceIncomplete("stale_handle", "prepared graph expired"), observed(False, False))
    session = root_session(client)

    with observing(planned(READ, EDIT)):
        assert session.has_read("README.md") is False

    assert sent(client) == [(READ, [EDIT]), (READ, [EDIT])]
    assert session.graph.lapses == 1


def test_an_observation_that_outlives_a_graph_lapse_is_not_recorded():
    client = ObservingClient(
        EvidenceIncomplete("stale_handle", "prepared graph expired"),
        observed(False, True),
        {"kind": "scalar", "value": False},
    )
    root = root_session(client)
    reader, editor = root.retain(), root.retain()

    with observing(planned(READ, EDIT)) as plan:
        assert reader.has_read("README.md") is False
        assert root.graph.lapses == 1
        assert plan.outcomes == {}
        assert editor.has_tool("Edit") is False
        assert plan.outcomes == {scalar_key(editor, EDIT): False}

    assert sent(client) == [(READ, [EDIT]), (READ, [EDIT]), (EDIT, [READ])]


def test_turn_and_transcript_scopes_never_share_observations():
    client = ObservingClient(observed(False, False), {"kind": "scalar", "value": True})
    session = root_session(client)

    with observing(planned(READ, EDIT) + planned(EDIT, selectors=TURN)):
        assert session.has_read("README.md") is False
        assert session.current_turn.has_tool("Edit") is True
        assert session.has_tool("Edit") is False

    assert [arguments["selectors"] for arguments in graph_queries(client)] == [[], [{"kind": "current_turn"}]]
    assert sent(client) == [(READ, [EDIT]), (EDIT, None)]


def test_an_identical_predicate_on_another_branch_reuses_the_demanded_answer():
    client = ObservingClient({"kind": "scalar", "value": True})
    root = root_session(client)
    first, second = root.retain(), root.retain()

    with observing(planned(EDIT, EDIT)) as plan:
        assert plan.predicates == ((TRANSCRIPT, canonical_json(EDIT)),)
        assert first.has_tool("Edit") is True
        assert second.has_tool("Edit") is True

    assert sent(client) == [(EDIT, None)]


@pytest.mark.parametrize(
    "primary",
    [
        pytest.param(REGEX, id="regex"),
        pytest.param(EDIT_GLOB, id="edit-glob"),
        pytest.param(READ_GLOB, id="read-glob"),
    ],
)
def test_an_unplanned_primary_carries_observers_and_is_not_recorded(primary):
    client = ObservingClient(observed(True, None))
    session = root_session(client)

    with observing(planned(READ)) as plan:
        assert session.query(primary) is True
        assert plan.outcomes == {}

    assert sent(client) == [(primary, [READ])]


def test_without_a_plan_requests_are_unchanged_and_nothing_is_observed():
    client = ObservingClient()
    session = root_session(client)

    with observing([]) as plan:
        assert plan is None
        assert OBSERVER_PLAN.get() is None
        assert session.has_read("README.md") is False
        assert session.has_edit_to("src/**") is False

    assert [list(arguments) for arguments in graph_queries(client)] == [
        ["handle", "selectors", "query", "deadline_unix_ms", "limits"],
        ["handle", "selectors", "query", "deadline_unix_ms", "limits"],
    ]


def test_shallow_and_input_queries_never_carry_observers():
    client = ObservingClient()
    session = root_session(client)

    with observing(planned(READ, EDIT)) as plan:
        assert session.has_read("README.md", subagents=False) is True
        assert list(session.deep_inputs()) == [False]
        assert plan.outcomes == {}

    assert [(operation, "observers" in arguments) for operation, arguments in client.requests] == [
        ("query", False),
        ("prepare_graph", False),
        ("query_graph", False),
    ]


def test_only_the_first_observer_limit_siblings_ride_along_in_plan_order():
    tools = [tool_predicate("has_tool", f"Tool{index}", subagents=True) for index in range(OBSERVER_LIMIT + 8)]
    client = ObservingClient()
    session = root_session(client)

    with observing(planned(READ, *tools)):
        assert session.has_read("README.md") is False

    assert sent(client) == [(READ, tools[:OBSERVER_LIMIT])]


def test_siblings_ride_along_only_while_they_fit_the_observer_byte_budget():
    small = tool_predicate("has_tool", "Read", subagents=True)
    fits = tool_predicate("has_tool", "x" * (OBSERVER_BYTES // 2), subagents=True)
    overflows = tool_predicate("has_tool", "z" * (OBSERVER_BYTES // 2), subagents=True)
    oversized = tool_predicate("has_tool", "y" * OBSERVER_BYTES, subagents=True)
    client = ObservingClient()
    session = root_session(client)

    with observing(planned(READ, oversized, small, fits, overflows, EDIT)):
        assert session.has_read("README.md") is False

    assert sent(client) == [(READ, [small, fits])]


@pytest.mark.parametrize(
    "rejected",
    [
        pytest.param(REGEX, id="regex"),
        pytest.param(EDIT_GLOB, id="edit-glob"),
        pytest.param(READ_GLOB, id="read-glob"),
        pytest.param(
            list_predicate("has_command", [f"token-{index}" for index in range(257)], subagents=True),
            id="value-bound",
        ),
    ],
)
def test_a_sibling_the_contract_rejects_never_rides_along(rejected):
    client = ObservingClient()
    session = root_session(client)

    with observing(planned(READ, rejected, EDIT)):
        assert session.has_read("README.md") is False

    assert sent(client) == [(READ, [EDIT])]


@pytest.mark.parametrize("condition, predicted", PLANNED)
def test_the_plan_predicts_each_builtin_deep_predicate(condition, predicted):
    assert list(planned_predicates(spec(condition))) == [predicted]


@pytest.mark.parametrize("condition, predicted", PLANNED)
def test_the_plan_predicts_exactly_what_the_condition_sends(condition, predicted):
    client = ObservingClient()
    evt = make_pre_tool_event(ctx=build_ctx(transcript=root_session(client)))

    assert check_condition(condition, evt) is False
    assert [
        (canonical_json(arguments["selectors"]), canonical_json(arguments["query"]))
        for arguments in graph_queries(client)
    ] == [predicted]


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param(RanCommand(Regex(r"\bpytest\b")), id="regex-command"),
        pytest.param(ReadFile("*.md", "**/docs/**"), id="read-file"),
        pytest.param(TouchedFile("src/**", subagents=True), id="touched-file"),
        pytest.param(UsedSkill("codex", subagents=False), id="shallow-skill"),
        pytest.param(UsedTool("Read", subagents=False), id="shallow-tool"),
        pytest.param(ReadFile("*.md", subagents=False), id="shallow-read"),
        pytest.param(TouchedFile("src/**"), id="shallow-touched"),
        pytest.param(RanCommand("git", "push", subagents=False), id="shallow-command"),
        pytest.param(Or(RanCommand("git", "push")), id="command-under-or"),
        pytest.param(Tool("Bash"), id="event-condition"),
        pytest.param(Exploding(), id="custom"),
    ],
)
def test_the_plan_skips_what_can_never_ride_as_an_observer(condition):
    assert list(planned_predicates(spec(condition))) == []
    assert list(planned_predicates(spec(skip_if=[condition]))) == []


def test_a_tail_window_hook_plans_nothing():
    windowed = HookSpec(events=Event.PreToolUse, only_if=(UsedTool("Read"),), transcript_events=64)

    assert list(planned_predicates(windowed)) == []


def test_the_plan_recurses_with_each_combinators_own_meaning():
    assert list(
        planned_predicates(
            spec(
                Exploding(),
                And(UsedTool("Read"), Not(ReadFile("*.md")), RanCommand("git", "push"), Exploding()),
                Or(UsedSkill("codex"), RanCommand("make"), And(RanCommand("pytest")), Exploding()),
                skip_if=[
                    TouchedFile("src/**", subagents=True),
                    RanCommand("ruff"),
                    Not(RanCommand("uv", "sync")),
                    Exploding(),
                ],
            )
        )
    ) == [
        (TURN, '{"kind":"has_tool","pattern":"Read","subagents":true}'),
        (TRANSCRIPT, '{"kind":"has_command","subagents":true,"values":["git","push"]}'),
        (TURN, '{"kind":"has_skill_suffix","subagents":true,"values":["codex"]}'),
        (TRANSCRIPT, '{"kind":"has_command","subagents":true,"values":["pytest"]}'),
        (TRANSCRIPT, '{"kind":"has_command","subagents":true,"values":["uv","sync"]}'),
    ]


def test_a_command_under_or_sends_the_inputs_walk_the_plan_leaves_out():
    client = ObservingClient({"kind": "strings", "values": []})
    evt = make_pre_tool_event(ctx=build_ctx(transcript=root_session(client)))
    condition = Or(RanCommand("git", "push"))

    assert check_condition(condition, evt) is False
    assert sent(client) == [({"kind": "deep_predicate_inputs", "order": "forward"}, None)]
    assert list(planned_predicates(spec(condition))) == []


def test_the_planned_selectors_are_the_canonical_scope_keys():
    assert (TRANSCRIPT_SELECTORS, TURN_SELECTORS) == (TRANSCRIPT, TURN)


@pytest.mark.parametrize(
    "primary",
    [
        pytest.param(READ, id="read"),
        pytest.param(REGEX, id="regex"),
        pytest.param(EDIT_GLOB, id="edit-glob"),
        pytest.param(READ_GLOB, id="read-glob"),
    ],
)
def test_the_host_schema_accepts_eligible_observers_under_any_primary(primary):
    from captain_hook.snapshots.validation import validate

    validate("host-request", graph_request(query=primary))
    validate("host-request", graph_request(query=primary, observers=[EDIT, SKILL]))


@pytest.mark.parametrize(
    "observers",
    [
        pytest.param([REGEX], id="regex"),
        pytest.param([EDIT_GLOB], id="edit-glob"),
        pytest.param([READ_GLOB], id="read-glob"),
        pytest.param([], id="empty"),
    ],
)
def test_the_host_schema_refuses_observers_outside_the_contract(observers):
    from jsonschema import ValidationError

    from captain_hook.snapshots.validation import validate

    with pytest.raises(ValidationError):
        validate("host-request", graph_request(observers=observers))


def test_the_host_schema_accepts_an_observed_scalar_reply():
    from captain_hook.snapshots.validation import validate

    validate("host-response", response({"id": "graph-query"}, observed(False, True, False, None)))


def test_observer_constants_match_the_vendored_contract():
    from captain_hook.snapshots.validation import observers_schema, validator

    schema = observers_schema()
    definitions = validator("request").schema["$defs"]
    kinds = [
        definitions[variant["$ref"].rsplit("/", 1)[-1]]["properties"]["kind"] for variant in schema["items"]["oneOf"]
    ]

    assert schema["maxItems"] == OBSERVER_LIMIT
    assert {name for kind in kinds for name in kind.get("enum", [kind.get("const")])} == OBSERVABLE_KINDS
    assert (
        validator("response").schema["$defs"]["ObservedScalarResult"]["properties"]["observers"]["maxItems"]
        == OBSERVER_LIMIT
    )


def test_the_plan_is_active_only_while_conditions_match():
    from captain_hook.dispatch import prepare_hook_events

    seen = []
    probe = LambdaCondition(lambda evt: bool(seen.append(OBSERVER_PLAN.get())))
    register_hook(Event.PreToolUse, "fixture", only_if=[probe, UsedTool("Read")])

    assert prepare_hook_events(fixture_event(), async_=False) == ([], [])
    assert [plan.predicates for plan in seen] == [((TURN, '{"kind":"has_tool","pattern":"Read","subagents":true}'),)]
    assert OBSERVER_PLAN.get() is None


def test_no_plan_is_installed_without_a_deep_predicate():
    from captain_hook.dispatch import prepare_hook_events

    seen = []
    probe = LambdaCondition(lambda evt: bool(seen.append(OBSERVER_PLAN.get())))
    register_hook(Event.PreToolUse, "fixture", only_if=[probe, UsedTool("Read", subagents=False)])

    assert prepare_hook_events(fixture_event(), async_=False) == ([], [])
    assert seen == [None]


def test_the_plan_is_cleared_when_matching_raises():
    from captain_hook.dispatch import prepare_hook_events, surfacing_handler_errors

    def explode(evt):
        assert OBSERVER_PLAN.get() is not None
        raise RuntimeError("condition failed")

    register_hook(Event.PreToolUse, "fixture", only_if=[LambdaCondition(explode), UsedTool("Read")])

    with surfacing_handler_errors(), pytest.raises(RuntimeError, match="condition failed"):
        prepare_hook_events(fixture_event(), async_=False)
    assert OBSERVER_PLAN.get() is None


def test_the_pinned_producer_settles_planned_siblings_on_one_walk(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [raw_text("user", "go"), raw_assistant(raw_tool_use("Bash", {"command": "ls"}, "parent"))]
        )
    )
    children = tmp_path / "session" / "subagents"
    children.mkdir(parents=True)
    (children / "agent-child.jsonl").write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                raw_assistant(raw_tool_use("Bash", {"command": "git push origin main"}, "pushed")),
                raw_assistant(raw_tool_use("Read", {"file_path": "README.md"}, "read")),
            ]
        )
    )
    searched = tool_predicate("has_tool", "Grep", subagents=True)
    pushed = list_predicate("has_command", ("git", "push"), subagents=True)
    read = tool_predicate("has_tool", "Read", subagents=True)
    built = list_predicate("has_command", ("cargo", "build"), subagents=True)
    owner = FixtureOwner()
    try:
        session = owner.load(source)
        walks = []
        exchange = owner.client._exchange

        def record(wrapper):
            request = wrapper["request"]
            if request["operation"] == "query_graph":
                walks.append((request["query"], request.get("observers")))
            return exchange(wrapper)

        owner.client._exchange = record
        with observing(planned(searched, pushed, read, built)):
            assert session.has_tool("Grep") is False
            assert session.has_command("git", "push") is True
            assert session.has_tool("Read") is True
            assert session.has_command("cargo", "build") is False
        assert walks == [(searched, [pushed, read, built])]
    finally:
        owner.close()
