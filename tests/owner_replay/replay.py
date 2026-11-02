from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from captain_hook.snapshots.client import CORE_SCHEMA, HOST_SCHEMA, encode_frame, read_exact

FIXTURE = Path(__file__).with_name("fixture")
ROOT = "@ROOT@"
TOKEN = re.compile(r"[0-9a-f]{64}")
OUTPUT_TOKEN = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
DEADLINE_MS = 10 * 60 * 1000
MAX_EXTENSIONS = 64


class Unmapped(Exception):
    pass


def tokens(value: Any) -> Iterator[str]:
    match value:
        case dict():
            yield from (token for item in value.values() for token in tokens(item))
        case list():
            yield from (token for item in value for token in tokens(item))
        case str() if TOKEN.fullmatch(value):
            yield value


def result(frame: dict[str, Any]) -> dict[str, Any]:
    return {key: frame[key] for key in ("snapshot", "error") if key in frame}


def response(frame: dict[str, Any]) -> dict[str, Any]:
    return frame["snapshot"]["response"] if "snapshot" in frame else {"cursor": None}


@dataclass
class Replay:
    writer: IO[bytes]
    reader: IO[bytes]
    output: IO[str]
    root: str
    registries: list[list[dict[str, Any]]]
    contexts: list[dict[str, Any]]
    next_frame: int
    mapped: dict[str, str] = field(default_factory=dict)
    issued: set[str] = field(default_factory=set)
    finished: dict[str, dict[str, Any]] = field(default_factory=dict)
    ordinals: dict[str, str] = field(default_factory=dict)
    outcomes: Counter[tuple[str, str]] = field(default_factory=Counter)

    def send(self, frame: dict[str, Any]) -> None:
        self.writer.write(encode_frame(frame))
        self.writer.flush()

    def read(self) -> dict[str, Any]:
        return json.loads(read_exact(self.reader, struct.unpack(">I", read_exact(self.reader, 4))[0]))

    def receive(self, frame_id: int) -> dict[str, Any]:
        while (frame := self.read())["id"] != frame_id:
            self.emit({"unsolicited": frame})
        return frame

    def ordinal(self, match: re.Match[str]) -> str:
        return self.ordinals.setdefault(match[0], f"<T{len(self.ordinals) + 1}>")

    def emit(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.output.write(OUTPUT_TOKEN.sub(self.ordinal, line) + "\n")

    def rewrite(self, value: Any) -> Any:
        match value:
            case dict():
                return {key: self.rewrite(item) for key, item in value.items()}
            case list():
                return [self.rewrite(item) for item in value]
            case str() if TOKEN.fullmatch(value) and value in self.mapped:
                return self.mapped[value]
            case str() if TOKEN.fullmatch(value) and value in self.issued:
                raise Unmapped(value)
            case str() if value.startswith(ROOT):
                return self.root + value.removeprefix(ROOT)
            case _:
                return value

    def pair(self, recorded: Any, replayed: Any) -> None:
        match recorded:
            case dict() if isinstance(replayed, dict):
                for key in recorded:
                    self.pair(recorded[key], replayed.get(key))
            case list() if isinstance(replayed, list):
                for left, right in zip(recorded, replayed, strict=False):
                    self.pair(left, right)
            case str() if isinstance(replayed, str) and TOKEN.fullmatch(recorded):
                self.mapped[recorded] = replayed

    def call(
        self, frame_id: int, request: dict[str, Any], step: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        self.send(
            {
                "protocol": 1,
                "op": "snapshot_request",
                "id": frame_id,
                "snapshot": {
                    "schema": HOST_SCHEMA,
                    "request": request,
                    "tool_registry": self.registries[step["registry"]],
                },
                "snapshot_context": context,
            }
        )
        return self.receive(frame_id)

    def play(self, step: dict[str, Any]) -> None:
        if "cancel" in step:
            self.send({"protocol": 1, "op": "snapshot_cancel", "id": step["cancel"]})
            return
        request, recorded = step["request"], step["recorded"]
        operation = request["operation"]
        if operation == "resume" and request["cursor"] in self.finished and request["cursor"] not in self.mapped:
            self.pair(recorded, self.finished[request["cursor"]])
            self.settle(step, operation, "absorbed")
            return
        try:
            rewritten = self.rewrite(request)
        except Unmapped:
            self.settle(step, operation, "skipped")
            return
        context = self.contexts[step["context"]]
        context = context | {"authority": context["authority"] | {"effective_uid": str(os.geteuid())}}
        if "deadline_unix_ms" in rewritten:
            rewritten |= {"deadline_unix_ms": int(time.time() * 1000) + DEADLINE_MS}
        frame = self.call(step["id"], rewritten, step, context)
        self.emit({"id": step["id"]} | result(frame))
        replayed = response(frame)
        extensions = 0
        while recorded.get("cursor") is None and replayed["cursor"] is not None and extensions < MAX_EXTENSIONS:
            extensions += 1
            self.next_frame += 1
            resume = {"schema": CORE_SCHEMA, "id": f"{request['id']}~{extensions}", "operation": "resume"}
            frame = self.call(self.next_frame, resume | {"cursor": replayed["cursor"]}, step, context)
            self.emit({"id": step["id"], "extended": extensions} | result(frame))
            replayed = response(frame)
        self.pair(recorded, replayed)
        if recorded.get("cursor") is not None and replayed["cursor"] is None:
            self.finished[recorded["cursor"]] = replayed
            self.mapped.pop(recorded["cursor"], None)
        self.settle(step, operation, "extended" if extensions else "sent")

    def settle(self, step: dict[str, Any], operation: str, outcome: str) -> None:
        if outcome in {"absorbed", "skipped"}:
            self.emit({"id": step["id"], outcome: operation})
        self.issued |= set(tokens(step["recorded"]))
        self.outcomes[(operation, outcome)] += 1


def replay(argv: list[str], output: Path, build: str) -> int:
    hello = json.loads((FIXTURE / "hello.json").read_text())
    steps = [
        json.loads(line) for path in sorted(FIXTURE.glob("stream-*.jsonl")) for line in path.read_text().splitlines()
    ]
    scratch = Path(tempfile.gettempdir()) / "capt-hook-owner-replay"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(mode=0o700)
    with (
        output.open("w") as stream,
        output.with_suffix(".owner.log").open("wb") as log,
        subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=os.environ | {"TMPDIR": str(scratch)},
        ) as owner,
    ):
        session = Replay(
            writer=owner.stdin,
            reader=owner.stdout,
            output=stream,
            root=str((FIXTURE / "home").resolve()),
            registries=hello["tool_registries"],
            contexts=hello["snapshot_contexts"],
            next_frame=max(step.get("id", step.get("cancel", 0)) for step in steps),
        )
        session.send({"protocol": 1, "op": "hello", "build": build, "snapshot_config": hello["snapshot_config"]})
        session.emit({"hello": session.read()})
        for step in steps:
            session.play(step)
        session.writer.close()
        session.emit({"exit": owner.wait()})
    for (operation, outcome), count in sorted(session.outcomes.items()):
        print(f"{operation:>18} {outcome:<9} {count}", file=sys.stderr)
    return owner.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay the recorded snapshot-owner request stream against one owner process."
    )
    parser.add_argument("--out", type=Path, required=True, help="normalized response lines")
    parser.add_argument("--build", default=importlib.metadata.version("capt-hook"), help="hello build")
    parser.add_argument("owner", nargs="+", help="owner argv, after --")
    arguments = parser.parse_args()
    sys.exit(replay(arguments.owner, arguments.out, arguments.build))


if __name__ == "__main__":
    main()
