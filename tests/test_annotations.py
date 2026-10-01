from __future__ import annotations

from typing import Any

import pytest

from captain_hook import Annotated, T
from captain_hook.conditions import check_condition
from captain_hook.events import PreToolUseEvent
from tests.helpers import build_ctx, make_transcript


def event(tool_name: str, tool_input: dict[str, Any], *prompts: str) -> PreToolUseEvent:
    transcript = make_transcript(*(T.user(prompt) for prompt in prompts)) if prompts else None
    return PreToolUseEvent(
        _raw={"tool_name": tool_name, "tool_input": tool_input}, ctx=build_ctx(transcript=transcript)
    )


def bash(command: str) -> PreToolUseEvent:
    return event("Bash", {"command": command})


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("gt submit # ccx:raw", {"raw": None}),
        ("gt submit  #ccx:raw", {"raw": None}),
        ("gt submit # ccx:raw ccx:role=fix", {"raw": None, "role": "fix"}),
        ("gt submit # ccx:tooling-lane=ccx-raw:gh-pr", {"tooling-lane": "ccx-raw:gh-pr"}),
        ("bash -c 'gt submit # ccx:raw'", {"raw": None}),
        ("""bash -c "bash -c 'gt submit # ccx:raw'" """, {"raw": None}),
        ('eval "gt submit # ccx:raw"', {"raw": None}),
        ("x=$(gt submit # ccx:raw\n)", {"raw": None}),
    ],
)
def test_a_real_shell_comment_carries_annotations(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: dict[str, str | None]
) -> None:
    monkeypatch.delenv("CAPT_HOOK_CCX_RAW", raising=False)
    assert bash(command).annotations == expected


@pytest.mark.parametrize(
    "command",
    [
        "echo '# ccx:raw'",
        'git commit -m "skip # ccx:raw"',
        "cat <<EOF\n# ccx:raw\nEOF\ngt submit",
        "bash <<EOF\ngt submit # ccx:raw\nEOF",
        "zsh -c 'echo \"# ccx:raw\"; gt submit'",
        "gt submit # ccx:RAW",
        "gt submit # ccx: raw",
        "gt submit # see the ccx:raw= docs",
    ],
)
def test_text_that_is_not_a_comment_token_carries_nothing(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    monkeypatch.delenv("CAPT_HOOK_CCX_RAW", raising=False)
    assert bash(command).annotations == {}


@pytest.mark.parametrize(
    ("value", "raw"),
    [("1", True), ("true", True), ("YES", True), (" True ", True), ("0", False), ("false", False), ("", False)],
)
def test_the_raw_env_counts_only_a_truthy_value(monkeypatch: pytest.MonkeyPatch, value: str, raw: bool) -> None:
    monkeypatch.setenv("CAPT_HOOK_CCX_RAW", value)
    assert ("raw" in bash("gt submit").annotations) is raw
    assert ("raw" in event("Read", {"file_path": "/x"}).annotations) is raw


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Agent", {"prompt": "Fix the hook.\nccx: tooling-lane=github-quota role=evidence\nGo.", "description": "x"}),
        ("Task", {"prompt": "  ccx: tooling-lane=github-quota\trole=evidence  ", "description": "x"}),
        ("Skill", {"skill": "codex", "args": "ccx: tooling-lane=github-quota role=evidence"}),
    ],
)
def test_a_whole_dispatch_line_carries_annotations(tool_name: str, tool_input: dict[str, Any]) -> None:
    assert event(tool_name, tool_input).annotations == {"tooling-lane": "github-quota", "role": "evidence"}


@pytest.mark.parametrize(
    "prompt",
    [
        "Use ccx: role=evidence for this lane.",
        "ccx:role=evidence",
        "ccx: Role=evidence",
        "ccx: role=evidence, then ship",
    ],
)
def test_a_dispatch_line_must_be_whole_and_lowercase(prompt: str) -> None:
    assert event("Agent", {"prompt": prompt, "description": "x"}).annotations == {}


def test_a_bash_comment_annotation_never_reads_a_dispatch_line() -> None:
    assert bash("echo 'ccx: role=fix'").annotations == {}


def test_annotated_matches_key_and_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_CCX_RAW", raising=False)
    evt = bash("gt submit # ccx:raw ccx:role=fix")
    assert check_condition(Annotated("raw"), evt)
    assert check_condition(Annotated("role"), evt)
    assert check_condition(Annotated("role", "fix"), evt)
    assert not check_condition(Annotated("role", "evidence"), evt)
    assert not check_condition(Annotated("tooling-lane"), evt)


def test_session_scope_reads_the_dispatch_prompt() -> None:
    evt = event("Bash", {"command": "ccx vcs ship -m x"}, "ccx: role=fix\nShip the fix.", "ccx: role=other")
    assert check_condition(Annotated("role", "fix", scope="session"), evt)
    assert not check_condition(Annotated("role", "other", scope="session"), evt)
    assert not check_condition(Annotated("role"), evt)


def test_session_scope_ignores_prose_that_names_a_key() -> None:
    evt = event("Bash", {"command": "ccx vcs ship -m x"}, "Give this lane role=fix and ship.")
    assert not check_condition(Annotated("role", scope="session"), evt)
