from __future__ import annotations

import io
import os
import sys
from typing import Never

import pytest

from capt_hook_client import client, guard_literal
from tests.helpers import BENIGN_PAYLOAD, DESTRUCTIVE_PAYLOAD


class UnreadBuffer:
    def read(self, _size: int) -> Never:
        raise AssertionError("stdin must not be read")


class UnreadStdin:
    buffer = UnreadBuffer()


def missing_execv(_path: str, _argv: list[str]) -> Never:
    raise FileNotFoundError("missing")


def run(monkeypatch: pytest.MonkeyPatch, event: str, stdin: object) -> int:
    monkeypatch.setattr(sys, "argv", ["hook", "run", event])
    monkeypatch.setattr(sys, "stdin", stdin)
    with pytest.raises(SystemExit) as excinfo:
        client.main()
    return int(excinfo.value.code or 0)


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_missing_host_denies_a_destructive_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], event: str
) -> None:
    monkeypatch.setattr(os, "execv", missing_execv)
    assert run(monkeypatch, event, io.TextIOWrapper(io.BytesIO(DESTRUCTIVE_PAYLOAD))) == 0
    captured = capsys.readouterr()
    assert captured.out == guard_literal.deny_envelope(event, "host-unavailable") + "\n"
    assert captured.err == ""


def test_missing_host_stays_a_hook_error_for_a_benign_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os, "execv", missing_execv)
    assert run(monkeypatch, "PreToolUse", io.TextIOWrapper(io.BytesIO(BENIGN_PAYLOAD))) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"captain-hook client unavailable at {client.HOST}: missing\n"


def test_missing_host_never_reads_stdin_for_an_unguarded_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os, "execv", missing_execv)
    assert run(monkeypatch, "PostToolUse", UnreadStdin()) == 1
    assert capsys.readouterr().out == ""


def test_present_host_execs_with_stdin_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    def execv(path: str, argv: list[str]) -> Never:
        captured.extend((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", execv)
    assert run(monkeypatch, "PreToolUse", UnreadStdin()) == 0
    assert captured == [client.HOST, [client.HOST, "run", "PreToolUse"]]
