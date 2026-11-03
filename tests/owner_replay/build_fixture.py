from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import shutil
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).with_name("fixture")
ROOT = "@ROOT@"
TRANSCRIPT_HOME = "/home/replay"
DROPPED_OPERATIONS = frozenset({"prepare_review", "prepare_corrections"})
HEAD_BYTES = 12 * 1024
TAIL_BYTES = 36 * 1024
CODEX_THREADS = 8
STEPS_PER_FILE = 1000
TOKEN = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"
    r"|[0-9a-f]{16,}"
    r"|(toolu|msg|req|call|fc|resp|rs)_[A-Za-z0-9_-]+"
)
TAG = re.compile(r"(?<=<)/?[A-Za-z][\w-]*")
KEEP_KEYS = frozenset(
    {
        "type",
        "subtype",
        "role",
        "uuid",
        "parentUuid",
        "logicalParentUuid",
        "leafUuid",
        "sessionId",
        "session_id",
        "timestamp",
        "version",
        "userType",
        "entrypoint",
        "id",
        "tool_use_id",
        "toolUseID",
        "toolUseId",
        "sourceToolAssistantUUID",
        "sourceToolUseID",
        "promptId",
        "requestId",
        "messageId",
        "name",
        "model",
        "stop_reason",
        "stop_sequence",
        "hookEvent",
        "level",
        "call_id",
        "originator",
        "cli_version",
        "model_provider",
        "service_tier",
    }
)
OPAQUE_KEYS = frozenset({"input", "arguments", "toolUseResult"})
OPAQUE_KEEP_KEYS = frozenset({"type", "skill", "subagent_type"})
ID_KEYS = frozenset({"agentId"})
PATH_KEYS = frozenset({"cwd", "file_path", "filePath", "path", "notebook_path", "transcript_path"})
KEEP_COMPONENTS = frozenset({".claude", "projects", "subagents", ".codex", "sessions"})
KEEP_SEGMENTS = frozenset({"agent", "jsonl", "rollout"})
STRUCTURAL_COMPONENT = re.compile(
    r"\d+|(rollout-[\dT-]+-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.jsonl)?"
)


@dataclass(frozen=True)
class Recording:
    hello: dict[str, Any]
    requests: list[dict[str, Any]]
    responses: dict[int, dict[str, Any]]
    key: bytes


@dataclass(frozen=True)
class Scrubber:
    key: bytes
    home: str

    def filler(self, text: str) -> str:
        stream = hashlib.shake_256(hmac.new(self.key, text.encode(), hashlib.sha256).digest()).digest(len(text))
        kept = {index for match in TAG.finditer(text) for index in range(match.start(), match.end())}
        return "".join(
            char
            if index in kept or not char.isalnum()
            else chr(ord("0") + byte % 10)
            if char.isdigit()
            else chr(ord("A" if char.isupper() else "a") + byte % 26)
            for index, (char, byte) in enumerate(zip(text, stream, strict=True))
        )

    def component(self, name: str) -> str:
        if name in KEEP_COMPONENTS or STRUCTURAL_COMPONENT.fullmatch(name):
            return name
        return "".join(
            segment
            if segment in KEEP_SEGMENTS or not segment.isalnum() or re.fullmatch(r"\d+|[0-9a-f]{8,}", segment)
            else self.filler(segment)
            for segment in re.split(r"([-_.])", name)
        )

    def under_home(self, text: str) -> bool:
        return text == self.home or text.startswith(self.home + "/")

    def path(self, path: str, root: str) -> str:
        return root + "".join(f"/{self.component(part)}" for part in path.removeprefix(self.home).split("/")[1:])

    def value(self, value: Any, key: str | None = None, *, opaque: bool = False) -> Any:
        match value:
            case dict():
                return {
                    name: self.value(item, name, opaque=opaque or name in OPAQUE_KEYS) for name, item in value.items()
                }
            case list():
                return [self.value(item, key, opaque=opaque) for item in value]
            case str():
                return self.string(value, key, opaque=opaque)
            case _:
                return value

    def string(self, text: str, key: str | None, *, opaque: bool) -> str:
        if key in (OPAQUE_KEEP_KEYS if opaque else KEEP_KEYS) or IDENTIFIER.fullmatch(text):
            return text
        if key in ID_KEYS:
            return self.component(text)
        if self.under_home(text) and (key in PATH_KEYS or not re.search(r"\s", text)):
            return self.path(text, TRANSCRIPT_HOME)
        if text[:1] in "{[" and isinstance(parsed := loads(text), dict | list):
            return json.dumps(self.value(parsed, key, opaque=opaque), ensure_ascii=False, separators=(",", ":"))
        return self.filler(text)

    def transcript(self, line: bytes) -> bytes:
        text = line.decode(errors="replace").removesuffix("\n")
        return (
            json.dumps(self.value(parsed), ensure_ascii=False, separators=(",", ":"))
            if isinstance(parsed := loads(text), dict)
            else self.filler(text)
        ).encode() + b"\n"

    def pattern(self, pattern: str) -> str:
        pieces = re.findall(r"\\.|.", pattern, flags=re.DOTALL)
        literal = "".join(piece[-1] for piece in pieces).removeprefix("^").removesuffix("$")
        replacement = iter(char for char in self.filler(literal) if char.isalnum())
        return "".join(next(replacement) if piece.isalnum() else piece for piece in pieces)

    def request(self, value: Any) -> Any:
        match value:
            case {"field": str(), "flags": int(), "pattern": str(pattern)}:
                return value | {"pattern": self.pattern(pattern)}
            case dict():
                return {name: self.request(item) for name, item in value.items()}
            case list():
                return [self.request(item) for item in value]
            case str() if self.under_home(value):
                return self.path(value, ROOT)
            case _:
                return value


def loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def frames(path: Path) -> Iterator[tuple[bytes, dict[str, Any]]]:
    data = path.read_bytes()
    offset = 0
    while offset + 4 <= len(data):
        end = offset + 4 + struct.unpack(">I", data[offset : offset + 4])[0]
        if end > len(data):
            return
        yield data[offset + 4 : end], json.loads(data[offset + 4 : end])
        offset = end


def tokens(value: Any) -> Iterator[str]:
    match value:
        case dict():
            yield from (token for item in value.values() for token in tokens(item))
        case list():
            yield from (token for item in value for token in tokens(item))
        case str() if TOKEN.fullmatch(value):
            yield value


def record(prefix: Path, count: int) -> Recording:
    inbound = list(frames(prefix.with_suffix(".in")))
    selected = inbound[1 : count + 1]
    return Recording(
        hello=inbound[0][1],
        requests=[frame for _, frame in selected],
        responses={
            frame["id"]: frame
            for _, frame in reversed(list(frames(prefix.with_suffix(".out"))))
            if frame.get("op") in {"snapshot_result", "error"}
        },
        key=hashlib.sha256(b"".join(payload for payload, _ in selected)).digest(),
    )


def kept_requests(recording: Recording) -> list[dict[str, Any]]:
    dropped: set[str] = set()
    kept = []
    for frame in recording.requests:
        if frame["op"] == "snapshot_request" and (
            frame["snapshot"]["request"]["operation"] in DROPPED_OPERATIONS
            or dropped & set(tokens(frame["snapshot"]["request"]))
        ):
            dropped |= set(tokens(recording.responses[frame["id"]])) - set(tokens(frame["snapshot"]["request"]))
            continue
        kept.append(frame)
    return kept


def skeleton(value: Any, referenced: set[str]) -> Any:
    match value:
        case dict():
            return {
                key: pruned for key, item in value.items() if (pruned := skeleton(item, referenced)) is not None
            } or None
        case list():
            return [skeleton(item, referenced) for item in value] if set(tokens(value)) & referenced else None
        case str() if value in referenced:
            return value
        case _:
            return None


def recorded(frame: dict[str, Any], referenced: set[str]) -> dict[str, Any]:
    if frame["op"] == "error":
        return {}
    response = frame["snapshot"]["response"]
    return (skeleton(response, referenced) or {}) | ({"cursor": cursor} if (cursor := response["cursor"]) else {})


def complete(data: bytes) -> list[bytes]:
    return [line + b"\n" for line in data.split(b"\n")[:-1]]


def lines(path: Path, size: int) -> list[bytes]:
    with path.open("rb") as stream:
        if size <= HEAD_BYTES + TAIL_BYTES:
            return complete(stream.read(size))
        head = complete(stream.read(HEAD_BYTES))
        stream.seek(size - TAIL_BYTES)
        return head + complete(stream.read(TAIL_BYTES))[1:]


def described(value: Any) -> Iterator[tuple[str, int]]:
    match value:
        case {"canonical_path": str(path), "source_bytes": int(size)}:
            yield path, size
        case dict():
            yield from (pair for item in value.values() for pair in described(item))
        case list():
            yield from (pair for item in value for pair in described(item))


def recorded_sizes(recording: Recording) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for frame in recording.requests:
        if frame["op"] == "snapshot_request" and recording.responses[frame["id"]]["op"] == "snapshot_result":
            response = recording.responses[frame["id"]]["snapshot"]["response"]
            tail = (
                [(frame["snapshot"]["request"]["path"], response["data"]["source_bytes"])]
                if isinstance(response["data"], dict) and response["data"].get("kind") == "tail"
                else []
            )
            for path, size in [*described(response), *tail]:
                sizes[path] = max(sizes.get(path, 0), size)
    return sizes


def codex_threads(home: Path, requests: list[dict[str, Any]]) -> dict[str, Path]:
    found = {path.stem[-36:]: path for path in (home / ".codex/sessions").glob("*/*/*/rollout-*.jsonl")}
    newest = {
        max(present)
        for frame in requests
        if frame["op"] == "snapshot_request"
        if (present := [thread for thread in frame["snapshot"]["request"].get("thread_ids", []) if thread in found])
    }
    return {thread: found[thread] for thread in sorted(newest, reverse=True)[:CODEX_THREADS]}


def sources(requests: list[dict[str, Any]], scrubber: Scrubber) -> set[Path]:
    return {
        Path(path)
        for frame in requests
        if frame["op"] == "snapshot_request"
        for request in [frame["snapshot"]["request"]]
        for path in [request.get("path"), *request.get("direct_paths", [])]
        if path and scrubber.under_home(path)
    }


def trimmed(request: dict[str, Any], threads: dict[str, Path]) -> dict[str, Any]:
    return (
        request | {"thread_ids": [thread for thread in request["thread_ids"] if thread in threads]}
        if "thread_ids" in request
        else request
    )


def write_tree(paths: set[Path], sizes: dict[str, int], scrubber: Scrubber) -> int:
    total = 0
    for source in sorted(path for path in paths if path.is_file()):
        target = FIXTURE / "home" / scrubber.path(str(source), "").lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        body = lines(source, sizes.get(str(source), source.stat().st_size))
        total += target.write_bytes(b"".join(scrubber.transcript(line) for line in body))
    return total


def interned(value: Any, table: list[Any]) -> int:
    if value not in table:
        table.append(value)
    return table.index(value)


def step(
    frame: dict[str, Any],
    recording: Recording,
    scrubber: Scrubber,
    threads: dict[str, Path],
    tables: dict[str, list[Any]],
    referenced: set[str],
) -> dict[str, Any]:
    if frame["op"] == "snapshot_cancel":
        return {"cancel": frame["id"]}
    return {
        "id": frame["id"],
        "request": scrubber.request(trimmed(frame["snapshot"]["request"], threads)),
        "registry": interned(frame["snapshot"]["tool_registry"], tables["tool_registries"]),
        "context": interned(frame["snapshot_context"], tables["snapshot_contexts"]),
        "recorded": recorded(recording.responses[frame["id"]], referenced),
    }


def build(prefix: Path, count: int) -> None:
    recording = record(prefix, count)
    scrubber = Scrubber(recording.key, str(Path.home()))
    requests = kept_requests(recording)
    threads = codex_threads(Path.home(), requests)
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir()
    tree_bytes = write_tree(sources(requests, scrubber) | set(threads.values()), recorded_sizes(recording), scrubber)
    tables: dict[str, list[Any]] = {"tool_registries": [], "snapshot_contexts": []}
    referenced = {
        token
        for frame in requests
        if frame["op"] == "snapshot_request"
        for token in tokens(frame["snapshot"]["request"])
    }
    steps = [step(frame, recording, scrubber, threads, tables, referenced) for frame in requests]
    (FIXTURE / "hello.json").write_text(
        json.dumps({"snapshot_config": recording.hello["snapshot_config"]} | tables, indent=2, sort_keys=True) + "\n"
    )
    for index in range(0, len(steps), STEPS_PER_FILE):
        (FIXTURE / f"stream-{index // STEPS_PER_FILE:02d}.jsonl").write_text(
            "".join(
                json.dumps(step, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for step in steps[index : index + STEPS_PER_FILE]
            )
        )
    print(f"{len(steps)} steps, {tree_bytes} transcript bytes, {len(threads)} codex threads")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the scrubbed snapshot-owner replay fixture from a recording.")
    parser.add_argument("prefix", type=Path, help="recording path without its .in/.out suffix")
    parser.add_argument("--frames", type=int, default=2000, help="request frames to keep after the hello")
    arguments = parser.parse_args()
    build(arguments.prefix, arguments.frames)


if __name__ == "__main__":
    main()
