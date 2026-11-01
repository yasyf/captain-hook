from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from captain_hook import BaseHookEvent, CustomCondition, Event, hook
from captain_hook.app import State, current_state, use_state
from captain_hook.context import HookContext
from captain_hook.search_guard import check, main


@pytest.fixture
def search_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    repo = home / "Code" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "focused").mkdir(parents=True)
    (home / "repos").mkdir()
    monkeypatch.setenv("HOME", str(home))
    return repo


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("search guard invoked unrelated discovery, evidence, or a subprocess")

    monkeypatch.setattr("captain_hook.loader.discover_hooks", unexpected)
    monkeypatch.setattr("captain_hook.loader.discover_pack", unexpected)
    monkeypatch.setattr("captain_hook.cli.CliState.discover", unexpected)
    monkeypatch.setattr("captain_hook.snapshots.client.client_scope", unexpected)
    monkeypatch.setattr("captain_hook.snapshots.client.SnapshotClient.__init__", unexpected)
    monkeypatch.setattr("subprocess.Popen", unexpected)
    original = HookContext.__getattribute__

    def no_transcript(self: HookContext, name: str) -> Any:
        if name in {"transcript", "root_transcript", "t", "turn", "prior"}:
            pytest.fail("search guard accessed transcript evidence")
        return original(self, name)

    monkeypatch.setattr(HookContext, "__getattribute__", no_transcript)


def payload(command: str, root: Path) -> dict[str, Any]:
    return {
        "session_id": "native-search-guard",
        "turn_id": "native-turn",
        "tool_use_id": "native-tool",
        "model": "gpt-6.1-sol",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PreToolUse",
        "cwd": str(root),
        "transcript_path": str(root / "must-not-open.jsonl"),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


@pytest.mark.parametrize(
    "command",
    [
        "rg --files -g AGENTS.md -g CLAUDE.md {home}/repos {home} 2>/dev/null",
        "rg --files -g '*.py' {home}",
        "rg needle .",
        "grep -r needle src/focused",
        "find {home} -type f -name '*.py'",
    ],
)
def test_native_bash_payload_denies_existing_broad_searches(search_repo: Path, command: str) -> None:
    output = check(payload(command.format(home=Path.home()), search_repo))
    assert output is not None
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "rg --files .",
        "rg --files src/focused",
        "rg needle src/focused",
        "rg -g '*.py' needle .",
        "find src/focused -type f",
        "printf safe",
    ],
)
def test_native_bash_payload_preserves_focused_searches(search_repo: Path, command: str) -> None:
    assert check(payload(command, search_repo)) is None


def test_unrelated_native_tools_are_not_reinterpreted(search_repo: Path) -> None:
    raw = payload("rg --files ~", search_repo)
    raw["tool_name"] = "Read"
    raw["tool_input"] = {"file_path": str(search_repo / "file.py")}
    assert check(raw) is None


def test_search_guard_preserves_outer_registry(search_repo: Path) -> None:
    with use_state(State()) as outer:
        hook(Event.PreToolUse, message="unrelated policy must not run", block=True)
        before = list(outer.hooks)
        assert check(payload("printf safe", search_repo)) is None
        assert current_state() is outer
        assert outer.hooks == before


def test_search_guard_keeps_loaded_pack_anchors_without_running_old_rules(search_repo: Path, tmp_path: Path) -> None:
    class UnexpectedCondition(CustomCondition):
        def check(self, evt: BaseHookEvent) -> bool:
            pytest.fail("unrelated condition ran")

    pack = tmp_path / "outside" / "loaded-pack"
    pack.mkdir(parents=True)
    raw = payload(f"rg needle {pack}", search_repo)
    with use_state(State()):
        assert check(raw) is None
    with use_state(State()) as outer:
        hook(Event.Stop, message="old nonmatching event", block=True)
        hook(Event.PreToolUse, only_if=[UnexpectedCondition()], message="unrelated policy", block=True)
        outer.hooks[-1] = replace(outer.hooks[-1], state_root=str(pack), pack_name="foreign")
        before = list(outer.hooks)
        output = check(raw)
        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert current_state() is outer
        assert outer.hooks == before


def test_entrypoint_emits_only_supported_denial_json(
    search_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = payload("rg --files -g AGENTS.md ~", search_repo)
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(raw)))
    main()
    captured = capsys.readouterr()
    assert captured.err == ""
    output = json.loads(captured.out)
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert raw["tool_input"]["command"] not in captured.out


def test_entrypoint_allowed_command_has_no_approval_side_effect(
    search_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload("printf safe", search_repo))))
    main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_entrypoint_keeps_json_parse_failure_visible(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", StringIO("{"))
    with pytest.raises(json.JSONDecodeError):
        main()
    assert capsys.readouterr().out == ""


def test_cli_uses_the_search_only_entrypoint(search_repo: Path) -> None:
    from captain_hook.cli import cli

    result = CliRunner().invoke(cli, ["search-guard"], input=json.dumps(payload("rg --files ~", search_repo)))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_standalone_matches_all_registered_search_fixtures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from captain_hook.conditions import matches_conditions
    from captain_hook.envelopes import block_permission_envelope, join_block_messages
    from captain_hook.loader import import_pack_module, stamp_identity
    from captain_hook.search_guard import PERFORMANCE_ROOT
    from captain_hook.testing.helpers import mock_tool_event

    with use_state(State()) as state:
        import_pack_module("captain_hook._search_guard_parity", PERFORMANCE_ROOT / "search.py")
        stamp_identity(0, root=PERFORMANCE_ROOT, pack="performance")
        assert len(state.hooks) == 4
        for entry in state.hooks:
            assert entry.spec.tests
            for inp in entry.spec.tests:
                event = mock_tool_event(
                    tool=inp.tool or "Bash", command=inp.command, tool_input=inp.tool_input, cwd=inp.cwd
                )
                messages = tuple(
                    candidate.spec.message
                    for candidate in state.hooks
                    if candidate.spec.message and matches_conditions(candidate.spec, event)
                )
                expected = (
                    block_permission_envelope("PreToolUse", join_block_messages(messages, ())) if messages else None
                )
                assert check(event._raw) == expected
                monkeypatch.setattr("sys.stdin", StringIO(json.dumps(event._raw)))
                main()
                output = capsys.readouterr()
                assert output.err == ""
                assert (json.loads(output.out) if output.out else None) == expected
