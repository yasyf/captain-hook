from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from click.testing import CliRunner

from captain_hook.cli import cli
from captain_hook.pause import MAX_PAUSE, parse_duration, pause_path


def invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


@pytest.mark.parametrize(
    ("text", "span"),
    [
        pytest.param("30m", timedelta(minutes=30), id="minutes"),
        pytest.param("1h", timedelta(hours=1), id="hours"),
        pytest.param("45s", timedelta(seconds=45), id="seconds"),
        pytest.param("0h30m15s", timedelta(minutes=30, seconds=15), id="compound"),
    ],
)
def test_parse_duration_reads_hours_minutes_and_seconds(text: str, span: timedelta) -> None:
    assert parse_duration(text) == span


@pytest.mark.parametrize("text", ["", "0m", "90", "1d", "2h", "1h1s", "m30"])
def test_parse_duration_rejects_anything_but_a_span_within_the_cap(text: str) -> None:
    with pytest.raises(Exception, match="duration|at most"):
        parse_duration(text)


def test_pause_writes_the_expiry_the_host_reads() -> None:
    before = int(time.time())

    code, output = invoke("pause", "--for", "30m", "--reason", "cpu saturation")

    assert code == 0
    assert "hooks paused until" in output
    assert "capt-hook resume" in output
    record = json.loads(pause_path().read_text())
    assert record["reason"] == "cpu saturation"
    assert before + 1800 <= record["until"] <= int(time.time()) + 1800


def test_status_reports_the_pause_and_resume_lifts_it() -> None:
    invoke("pause", "--for", "10m", "--reason", "cpu")

    assert "hooks paused until" in invoke("pause", "--status")[1]
    assert "(cpu)" in invoke("pause", "--status")[1]
    assert invoke("resume") == (0, "hooks resumed\n")
    assert not pause_path().exists()
    assert invoke("pause", "--status") == (0, "hooks are not paused\n")
    assert invoke("resume") == (0, "hooks were not paused\n")


@pytest.mark.parametrize(
    "until",
    [
        pytest.param(lambda: int(time.time()) - 1, id="expired"),
        pytest.param(lambda: int(time.time() + MAX_PAUSE.total_seconds()) + 60, id="beyond-the-cap"),
    ],
)
def test_status_ignores_a_pause_the_host_ignores(until) -> None:
    pause_path().parent.mkdir(parents=True, exist_ok=True)
    pause_path().write_text(json.dumps({"until": until(), "reason": None}))

    assert invoke("pause", "--status") == (0, "hooks are not paused\n")


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("pause",), id="no-duration"),
        pytest.param(("pause", "--for", "2h"), id="over-the-cap"),
        pytest.param(("pause", "--for", "soon"), id="not-a-duration"),
        pytest.param(("pause", "--for", "10m", "--status"), id="both"),
    ],
)
def test_pause_refuses_without_a_valid_duration(args: tuple[str, ...]) -> None:
    code, _ = invoke(*args)

    assert code == 2
    assert not pause_path().exists()
