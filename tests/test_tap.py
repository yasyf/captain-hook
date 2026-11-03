from __future__ import annotations

import subprocess
from typing import Any

import pytest

from captain_hook import Event, Input
from captain_hook.primitives.tap import CARD_COMMAND_ENV, queue_card
from captain_hook.testing.helpers import input_to_event


def test_the_card_poster_runs_as_the_calling_session_and_receives_the_exact_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv(CARD_COMMAND_ENV, "post-card --kind ask")
    evt = input_to_event(Event.PreToolUse, Input(command="cc-sudo run -- true", session_id="session-1", seen={}))

    assert queue_card(evt, "/usr/bin/true")
    assert queue_card(evt, "/usr/bin/true")

    assert len(calls) == 1
    argv, env = calls[0]
    assert argv[:3] == ["post-card", "--kind", "ask"]
    assert argv[3].endswith(": /usr/bin/true")
    assert env["CLAUDE_CODE_SESSION_ID"] == "session-1"
