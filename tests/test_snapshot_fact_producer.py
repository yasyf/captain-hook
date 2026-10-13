from __future__ import annotations

import gc
import importlib.metadata
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cc_transcript import _native

from captain_hook.snapshots import worker
from captain_hook.snapshots.client import encode_frame
from captain_hook.snapshots.validation import checked, validator
from captain_hook.snapshots.worker import Owner, fact_producer
from captain_hook.testing.snapshots import FixtureOwner
from tests.helpers import raw_assistant, raw_text, raw_tool_use

HOST = "capt-hook/9.9.9"
EXTENSION = b"fixture extension"
DIGEST = "62a4a7e64b905cef897394b77b63048cd47137349231bf45a15a6782261800a9"
CHANGED_DIGEST = "210162c824485affe869bd4a70bc49d30b3afb16db2fda9e46e65ed0630a2de9"


def defaults() -> dict[str, Any]:
    return {key: field["default"] for key, field in validator("config").schema["properties"].items()}


@pytest.mark.parametrize(
    ("name", "contents", "modified", "digest"),
    [
        pytest.param("_native.abi3.so", EXTENSION, 1_700_000_000, DIGEST, id="loaded-extension"),
        pytest.param("moved.so", EXTENSION, 1_800_000_000, DIGEST, id="same-bytes-at-another-path-and-mtime"),
        pytest.param("_native.abi3.so", b"fixture extensioN", 1_700_000_000, CHANGED_DIGEST, id="one-byte-changed"),
    ],
)
def test_fact_producer_names_the_bytes_of_the_loaded_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, contents: bytes, modified: int, digest: str
) -> None:
    extension = tmp_path / name
    extension.write_bytes(contents)
    os.utime(extension, (modified, modified))
    monkeypatch.setattr(_native, "__file__", str(extension))
    monkeypatch.setattr(importlib.metadata, "version", {"cc-transcript": "14.42.0"}.__getitem__)

    assert fact_producer(HOST) == f"capt-hook/9.9.9 cc-transcript/14.42.0 native/sha256:{digest}"


@pytest.mark.parametrize(
    ("offered", "version", "named"),
    [
        pytest.param(None, "14.42.0", None, id="unnamed"),
        pytest.param(HOST, "14.42.0", f"capt-hook/9.9.9 cc-transcript/14.42.0 native/sha256:{DIGEST}", id="named"),
        pytest.param(
            HOST,
            "14.42.1.dev7+g59765e4.d20261013",
            f"capt-hook/9.9.9 cc-transcript/14.42.1.dev7+g59765e4.d20261013 native/sha256:{DIGEST}",
            id="local-version",
        ),
    ],
)
def test_the_owner_completes_a_named_producer_before_it_builds_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offered: str | None, version: str, named: str | None
) -> None:
    extension = tmp_path / "_native.abi3.so"
    extension.write_bytes(EXTENSION)
    config = defaults() | {"prepared_fact_producer": offered}
    hello = {"protocol": 1, "op": "hello", "build": "9.9.9", "snapshot_config": config}
    stored: list[dict[str, Any]] = []

    def owner(completed: dict[str, Any]) -> SimpleNamespace:
        stored.append(completed)
        return SimpleNamespace(record_transport=lambda count: None, close=lambda: None)

    monkeypatch.setattr(_native, "__file__", str(extension))
    monkeypatch.setattr(importlib.metadata, "version", {"capt-hook": "9.9.9", "cc-transcript": version}.__getitem__)
    monkeypatch.setenv("CC_TRANSCRIPT_PARSE_THREADS", "4")
    monkeypatch.setattr(worker, "Owner", owner)

    with (
        (tmp_path / "stdout").open("w") as stdout,
        (tmp_path / "stderr").open("w") as stderr,
        monkeypatch.context() as streams,
    ):
        streams.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(encode_frame(hello))))
        streams.setattr(sys, "stdout", stdout)
        streams.setattr(sys, "stderr", stderr)
        worker.main()

    assert stored == [config | {"prepared_fact_producer": named}]
    assert checked("config", stored[0]) == stored[0]
    assert (tmp_path / "stdout").read_bytes() == encode_frame({"protocol": 1, "op": "hello", "build": "9.9.9"})


def test_the_pinned_store_opens_the_reuse_namespace_for_a_named_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    Owner(defaults() | {"prepared_fact_producer": fact_producer(HOST)})

    assert (tmp_path / "cc-transcript-prepared-reuse").is_dir()
    assert not (tmp_path / "cc-transcript-prepared").exists()


def test_a_restarted_owner_reads_the_facts_the_last_one_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    producer = fact_producer(HOST)
    monkeypatch.setattr(
        "captain_hook.testing.snapshots.Owner",
        lambda config: Owner(config | {"prepared_fact_producer": producer}),
    )
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

    def walk() -> tuple[list[dict[str, Any]], Counter[str]]:
        owner = FixtureOwner()
        replies: list[dict[str, Any]] = []
        exchange = owner.client._exchange

        def record(wrapper: dict[str, Any]) -> dict[str, Any]:
            reply = exchange(wrapper)
            if replies or wrapper["request"]["operation"] == "query_graph":
                replies.append(reply["response"])
            return reply

        try:
            session = owner.load(source)
            owner.client._exchange = record
            assert session.has_tool("Grep") is False
        finally:
            owner.close()
        return (
            [reply["data"] for reply in replies if reply["data"] is not None],
            sum((Counter(reply["usage"]) for reply in replies), Counter()),
        )

    built, building = walk()
    gc.collect()
    reused, reusing = walk()

    assert reused == built == [{"kind": "scalar", "value": False}]
    assert building["prepared_cache_reads"] == 0
    assert reusing["prepared_cache_reads"] >= 1
    assert reusing["prepared_cache_bytes_read"] > 0
    assert reusing["source_bytes_read"] < building["source_bytes_read"]
