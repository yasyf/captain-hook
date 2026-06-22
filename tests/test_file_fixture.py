from __future__ import annotations

import os

from cc_transcript.tools import ReadCall

from captain_hook.testing import FileFixture, Input
from captain_hook.testing.helpers import input_to_event, materialize_file
from captain_hook.types import Event


def test_file_fixture_size_materializes_real_file() -> None:
    path = materialize_file(FileFixture(size=12345))
    assert os.path.getsize(path) == 12345


def test_file_fixture_content_writes_bytes() -> None:
    path = materialize_file(FileFixture(content="abc"))
    with open(path, "rb") as f:
        assert f.read() == b"abc"


def test_file_fixture_name_is_honored() -> None:
    path = materialize_file(FileFixture(content="hi", name="readme.txt"))
    assert os.path.basename(path) == "readme.txt"


def test_read_event_from_file_fixture() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Read", file=FileFixture(size=12345)))
    assert evt.file is not None
    assert evt.file.path.exists()
    assert evt.file.path.stat().st_size == 12345
    assert isinstance(evt.input, ReadCall)
    assert evt.input.offset is None
    assert evt.input.limit is None


def test_read_event_carries_offset_and_limit() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Read", file="x.py", offset=10, limit=50))
    assert isinstance(evt.input, ReadCall)
    assert evt.input.offset == 10
    assert evt.input.limit == 50
    assert evt._raw["tool_input"]["offset"] == 10
    assert evt._raw["tool_input"]["limit"] == 50
