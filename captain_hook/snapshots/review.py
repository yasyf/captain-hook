from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from cc_transcript.context import ContextWindow
from cc_transcript.corrections import Correction
from cc_transcript.ids import EventRef
from cc_transcript.mining.candidates import FeedbackCandidate
from cc_transcript.mining.confidence import from_payload, to_payload
from cc_transcript.mining.signals import MiningSignal

from captain_hook.snapshots.client import MAX_RESULT_BYTES, EvidenceIncomplete
from captain_hook.snapshots.client import client_scope as review_client

REVIEW_POLICY = {"id": "captain-review", "version": "1"}


class ReviewPolicy:
    def __init__(self) -> None:
        self._decisions: OrderedDict[tuple[Path, int, int], Any] = OrderedDict()

    async def decision_log(self, path: Path | None) -> Any:
        from captain_hook.decisions import open_decision_log

        canonical = (path or Path.home() / ".cc-transcript" / "decisions.db").resolve()
        try:
            stat = canonical.stat()
            identity = (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            identity = None
        key = (canonical, *identity) if identity is not None else None
        if key is not None and key in self._decisions:
            self._decisions.move_to_end(key)
            return self._decisions[key]
        for old in list(self._decisions):
            if old[0] == canonical:
                await self._decisions.pop(old).close()
        if len(self._decisions) == 8:
            _, evicted = self._decisions.popitem(last=False)
            await evicted.close()
        log = await open_decision_log(canonical)
        stat = canonical.stat()
        if identity is not None and identity != (stat.st_dev, stat.st_ino):
            await log.close()
            raise EvidenceIncomplete("changed", "decision ledger was replaced while opening")
        self._decisions[canonical, stat.st_dev, stat.st_ino] = log
        return log

    async def prepare_review(self, snapshot: Any, request: Mapping[str, Any]) -> dict[str, Any]:
        return await prepare_review(snapshot, request, decision_log=self.decision_log)

    async def prepare_corrections(self, snapshot: Any, request: Mapping[str, Any]) -> dict[str, Any]:
        return await prepare_corrections(snapshot, request)

    async def close(self) -> None:
        while self._decisions:
            _, log = self._decisions.popitem()
            await log.close()


class ProjectionBudget:
    def __init__(self, *, maximum: int = MAX_RESULT_BYTES, snapshot: Any = None) -> None:
        self.remaining = maximum
        self.snapshot = snapshot

    def checkpoint(self) -> None:
        if self.snapshot is not None:
            self.snapshot.checkpoint()

    def check_text(self, text: str) -> None:
        self.checkpoint()
        if len(text) > self.remaining:
            self.exceeded()

    def exceeded(self) -> None:
        if self.snapshot is not None:
            self.snapshot.consume(output_bytes=MAX_RESULT_BYTES + 1)
        raise EvidenceIncomplete("output_limit", "review projection exceeds its cumulative output budget")

    def add(self, value: object) -> None:
        self.checkpoint()
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        if size > self.remaining:
            self.exceeded()
        if self.snapshot is not None:
            self.snapshot.consume(items=1, output_bytes=size)
        self.remaining -= size


class BoundedGit:
    def __init__(self, snapshot: Any, *, max_bytes: int, max_processes: int = 84) -> None:
        self.snapshot = snapshot
        self.remaining = max_bytes
        self.processes = max_processes

    def __call__(self, repo: Path, *arguments: str) -> str | None:
        self.snapshot.checkpoint()
        if self.processes == 0 or self.remaining == 0:
            raise EvidenceIncomplete("incomplete", "correction Git preparation exhausted its work budget")
        self.processes -= 1
        self.snapshot.consume(items=1)
        command = [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "log.showSignature=false",
            "-c",
            "color.ui=false",
            "-c",
            "submodule.recurse=false",
            "-C",
            str(repo),
            arguments[0],
            *(("--no-ext-diff", "--no-textconv") if arguments[0] in {"log", "show"} else ()),
            *arguments[1:],
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=os.environ | {"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
            )
        except OSError:
            return None
        deadline = time.monotonic() + 15
        chunks: list[bytes] = []
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    self.snapshot.checkpoint()
                    if time.monotonic() >= deadline:
                        raise EvidenceIncomplete("deadline", "correction Git preparation timed out")
                    for key, _ in selector.select(timeout=0.05):
                        chunk = os.read(key.fd, min(65536, self.remaining + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if len(chunk) > self.remaining:
                            raise EvidenceIncomplete("output_limit", "correction Git output exceeds preparation budget")
                        self.remaining -= len(chunk)
                        if key.data:
                            chunks.append(chunk)
                self.snapshot.checkpoint()
                return (
                    b"".join(chunks).decode(errors="replace")
                    if process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0
                    else None
                )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceIncomplete("deadline", "correction Git preparation timed out") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def review_roots(paths: Sequence[str]) -> list[str]:
    from cc_transcript import codex, discovery

    return sorted(
        {
            str(discovery.CLAUDE_PROJECTS_DIR),
            str(codex.sessions_root()),
            *(str(Path(path).parent) for path in paths if Path(path).is_absolute()),
        }
    )


def encode_candidate(signal: Any, candidate: FeedbackCandidate) -> str:
    from captain_hook.review.scan import rule_parts

    return json.dumps(
        {
            "dedup_key": candidate.dedup_key,
            "source_kind": candidate.source_kind,
            "occurred_at": candidate.occurred_at.isoformat(),
            "text": candidate.text,
            "context_json": candidate.window.to_json(),
            "signal": to_payload(candidate.signal),
            "session_id": candidate.session_id,
            "cc_version": candidate.cc_version,
            "payload": candidate.payload,
            "rule_parts": rule_parts(signal),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_candidate(raw: str) -> tuple[FeedbackCandidate, tuple[str, ...]]:
    data = json.loads(raw)
    window = ContextWindow.from_json(data["context_json"])
    return FeedbackCandidate(
        dedup_key=data["dedup_key"],
        source_kind=data["source_kind"],
        occurred_at=datetime.fromisoformat(data["occurred_at"]),
        text=data["text"],
        window=window,
        ref=window.anchor,
        signal=from_payload(data["signal"]),
        session_id=data["session_id"],
        cc_version=data["cc_version"],
        payload=data["payload"],
    ), tuple(data["rule_parts"])


async def prepare_review(snapshot: Any, request: Mapping[str, Any], *, decision_log: Any) -> dict[str, Any]:
    from cc_transcript.mining import mine_snapshot

    from captain_hook.review.fix import iter_hook_complaint_signals, prose_marker
    from captain_hook.review.routing import PackIndex
    from captain_hook.review.scan import (
        COLLAPSE_DETECTORS,
        REVIEWER_MARKER,
        REVIEWER_MINING_SPEC,
        Detector,
        resolve_repo_key,
        survives,
        to_candidate,
    )
    from captain_hook.util import reqenv

    if request["policy"] != REVIEW_POLICY:
        raise ValueError("unknown review policy")
    facts = snapshot.source_facts(first_user_contains=REVIEWER_MARKER)
    cwd = Path(facts["cwds"][0]) if facts["cwds"] else None
    repo = request.get("repo_key") or next(
        (key for path in facts["cwds"] if (key := resolve_repo_key(path)) is not None), None
    )
    disposition = "reviewer_session" if facts["first_user_contains"] else "no_repo" if repo is None else "eligible"
    result: dict[str, Any] = {
        "kind": "review",
        "canonical_path": snapshot.description["canonical_path"],
        "mtime_ns": snapshot.description["mtime_ns"],
        "repo_key": repo,
        "cwd": str(cwd) if cwd is not None else None,
        "disposition": disposition,
        "candidates_json": [],
    }
    if disposition != "eligible":
        return result
    budget = ProjectionBudget(maximum=request["limits"]["max_output_bytes"], snapshot=snapshot)
    budget.add(result)
    events = snapshot.events
    signals: list[MiningSignal] = []
    signal_chars = 0

    def keep(signal: Any) -> None:
        nonlocal signal_chars
        snapshot.checkpoint()
        floor = request["min_confidence_fix" if signal.kind == "hook_complaint" else "min_confidence"]
        if signal.signal.confidence < floor or not survives(events, signal):
            return
        signal_chars += len(signal.text)
        if signal_chars > budget.remaining or len(signals) >= request["limits"]["max_items"]:
            budget.exceeded()
        signals.append(signal)

    if any(
        prose_marker(row["role"], row["text"], is_sidechain=row["is_sidechain"], is_meta=row["is_meta"]) is not None
        for row in snapshot.prose_rows()
    ):
        with reqenv.use_request(
            reqenv.RequestOverrides(
                env={"CLAUDE_CONFIG_DIR": request["claude_config_dir"]},
                cwd=str(cwd or Path.home()),
                client_ppid=0,
                session_id="",
            )
        ):
            routing = PackIndex.load(cwd)
        decision_path = Path(request["decision_log_path"]) if request["decision_log_path"] is not None else None
        decisions = await decision_log(decision_path)
        async for signal in iter_hook_complaint_signals(events, decisions=decisions, index=routing):
            keep(signal)
    for signal in mine_snapshot(snapshot, REVIEWER_MINING_SPEC):
        keep(signal)
    shadowed = {
        (signal.session_id, signal.event_uuid, signal.text)
        for signal in signals
        if signal.detector in COLLAPSE_DETECTORS
    }
    for signal in signals:
        snapshot.checkpoint()
        if (
            signal.detector == Detector.TRANSCRIPT_MESSAGE
            and (signal.session_id, signal.event_uuid, signal.text) in shadowed
        ):
            continue
        [window] = snapshot.capture([EventRef(signal.session_id, signal.event_uuid)])
        raw = encode_candidate(signal, to_candidate(window, signal))
        budget.add(raw)
        result["candidates_json"].append(raw)

    return result


async def prepare_corrections(snapshot: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    from cc_transcript.evidence import harvest_pairs, lower_pair
    from cc_transcript.extract.correct import build_pick_prompt

    if request["policy"] != REVIEW_POLICY:
        raise ValueError("unknown correction policy")
    repo = Path(request["repo"]) if request["repo"] is not None else None
    drafts: list[dict[str, Any]] = []
    git = BoundedGit(
        snapshot, max_bytes=min(request["limits"]["max_read_bytes"], request["limits"]["max_output_bytes"])
    )
    budget = ProjectionBudget(maximum=request["limits"]["max_output_bytes"], snapshot=snapshot)
    for raw_anchor, feedback in zip(request["anchors"], request["feedback"], strict=True):
        snapshot.checkpoint()
        anchor = EventRef(**raw_anchor)
        activity = snapshot.activity(
            request["view"]["classifier"], anchor=anchor, lookback_turns=40, lookahead_turns=120
        )
        pairs = harvest_pairs(activity, anchor, repo=repo, git_runner=git)
        if not pairs or (turn := activity.turn_of(anchor)) is None:
            continue
        choices: list[dict[str, Any]] = []
        for index, pair in enumerate(pairs, 1):
            snapshot.checkpoint()
            hunk_chars = sum(len(hunk.old) + len(hunk.new) for hunk in pair.incorrect.hunks)
            if pair.correction is not None:
                hunk_chars += sum(len(hunk.old) + len(hunk.new) for hunk in pair.correction.hunks)
            if hunk_chars > budget.remaining:
                budget.exceeded()
            row = lower_pair(activity, anchor, pair, source="captain-hook")
            if row is None:
                raise ValueError("harvested correction pair lost its pinned anchor")
            if repo is not None:
                row = replace(row, detail={"repo": str(repo)})
            choice = {
                "pair_id": str(index),
                "overlap": pair.overlap,
                "correction_json": json.dumps(asdict(row), ensure_ascii=False, separators=(",", ":")),
            }
            budget.add(choice)
            choices.append(choice)
        draft = {
            "anchor": raw_anchor,
            "prompt": build_pick_prompt(feedback, pairs, anchor_turn=turn.index),
            "choices": choices,
        }
        budget.add({"anchor": raw_anchor, "prompt": draft["prompt"]})
        drafts.append(draft)
    return {"kind": "corrections", "corrections": drafts}


async def record_correction_drafts(drafts: Sequence[Mapping[str, Any]]) -> None:
    from cc_transcript.corrections import CorrectionLog
    from cc_transcript.extract.correct import CorrectionPick, usable_backend
    from spawnllm import extract

    if not drafts:
        return
    backend = usable_backend()
    async with await CorrectionLog.open() as log:
        for draft in drafts:
            anchor = draft["anchor"]
            if await log.for_anchor(anchor["session_id"], anchor["event_uuid"]):
                continue
            choices = draft["choices"]
            if not choices:
                continue
            if backend is None:
                chosen = max(choices, key=lambda choice: choice["overlap"])
            else:
                pick = await extract(draft["prompt"], CorrectionPick, backend=backend, model="medium")
                if pick.candidate is None or not 1 <= pick.candidate <= len(choices):
                    continue
                chosen = choices[pick.candidate - 1]
            await log.append(Correction(**json.loads(chosen["correction_json"])))


@dataclass(frozen=True, slots=True)
class RenderedEvidence:
    text: str
    reference: str


def render_review_windows(
    windows: Sequence[ContextWindow], *, roots: Sequence[str], render: Mapping[str, Any]
) -> list[RenderedEvidence | None | Exception]:
    from captain_hook.snapshots.client import NATIVE_CLASSIFIER, EvidenceIncomplete, Lease, SnapshotProtocolError

    rendered: list[RenderedEvidence | None | Exception] = [
        SnapshotProtocolError("window was not prepared") for _ in windows
    ]
    grouped: dict[str, list[tuple[int, ContextWindow]]] = {}
    for index, window in enumerate(windows):
        grouped.setdefault(window.anchor.session_id, []).append((index, window))
    with review_client() as client:
        for session_id, batch in grouped.items():
            leases: list[Lease] = []
            try:
                resolutions: list[dict[str, Any]] = []
                for page in client.pages(
                    "resolve", session_ids=[session_id], roots=list(roots), classifier=NATIVE_CLASSIFIER
                ):
                    for resolution in page["sessions"]:
                        resolutions.append(resolution)
                        if resolution["description"] is not None:
                            leases.append(Lease(client, resolution["description"]))
                if len(resolutions) != 1 or resolutions[0]["session_id"] != session_id:
                    raise SnapshotProtocolError("resolve did not return the requested session")
                resolution = resolutions[0]
                if resolution["status"] == "missing":
                    for index, _ in batch:
                        rendered[index] = None
                    continue
                if resolution["status"] != "ok" or len(leases) != 1:
                    raise EvidenceIncomplete("incomplete", "session resolution did not complete")
                for offset in range(0, len(batch), 256):
                    selected = batch[offset : offset + 256]
                    outputs = [
                        output
                        for page in client.pages(
                            "hydrate",
                            handles=[{"session_id": session_id, "handle": leases[0].require()}],
                            windows_json=[window.to_json() for _, window in selected],
                            render=dict(render),
                        )
                        for output in page["windows"]
                    ]
                    if len(outputs) != len(selected) or {item["input_index"] for item in outputs} != set(
                        range(len(selected))
                    ):
                        raise SnapshotProtocolError("hydrate did not cover every requested window")
                    by_index: dict[int, Any] = {output["input_index"]: output for output in outputs}
                    for position, (index, _) in enumerate(selected):
                        output = by_index[position]
                        if output["availability"] == "missing_ref":
                            rendered[index] = None
                        elif output["availability"] == "full" and isinstance(output["rendered"], str):
                            handle = leases[0].require()
                            rendered[index] = RenderedEvidence(
                                output["rendered"],
                                f"transcript:{handle['owner_epoch']}:{handle['snapshot_id']}:{handle['generation']}",
                            )
                        else:
                            raise SnapshotProtocolError("hydrate returned invalid availability")
            except EvidenceIncomplete as exc:
                for index, _ in batch:
                    rendered[index] = exc
            finally:
                for lease in leases:
                    lease.release()
    return rendered
