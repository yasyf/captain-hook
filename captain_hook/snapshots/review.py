from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cc_transcript.context import ContextWindow
from cc_transcript.corrections import Correction
from loguru import logger

from captain_hook.snapshots.client import MAX_RESULT_BYTES, EvidenceIncomplete, client_scope

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from cc_transcript.ids import EventRef
    from cc_transcript.mining.candidates import FeedbackCandidate
    from cc_transcript.models import TranscriptEvent

    from captain_hook.review.scan import CorrectionLedger
    from captain_hook.snapshots.client import RemoteSession, SnapshotClient

UNFAULTED = "none"
CORRECTION_PICK = (
    "Which numbered candidate edit does the developer's feedback fault? Pick none when the feedback is not about any "
    "of them."
)


def review_client() -> AbstractContextManager[SnapshotClient]:
    return client_scope(retry_retained=False)


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

    async def close(self) -> None:
        while self._decisions:
            _, log = self._decisions.popitem()
            await log.close()


class BoundedGit:
    def __init__(self, *, max_bytes: int, max_processes: int = 84) -> None:
        self.remaining = max_bytes
        self.processes = max_processes

    def __call__(self, repo: Path, *arguments: str) -> str | None:
        if self.processes == 0 or self.remaining == 0:
            raise EvidenceIncomplete("incomplete", "correction Git preparation exhausted its work budget")
        self.processes -= 1
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


def capture_windows(client: SnapshotClient, session: RemoteSession, anchors: Sequence[EventRef]) -> list[ContextWindow]:
    return [
        ContextWindow.from_json(raw)
        for offset in range(0, len(anchors), 256)
        for page in client.pages(
            "capture",
            view=session.view(),
            anchors=[asdict(anchor) for anchor in anchors[offset : offset + 256]],
            before=6,
            after=2,
            preview_chars=200,
        )
        for raw in page["windows_json"]
    ]


def correction_drafts(
    events: Sequence[TranscriptEvent], candidates: Sequence[FeedbackCandidate], repo: Path | None
) -> tuple[list[dict[str, Any]], list[tuple[EventRef, EvidenceIncomplete]]]:
    from cc_transcript.activity import SessionActivity
    from cc_transcript.evidence import harvest_pairs, lower_pair
    from cc_transcript.extract.correct import build_pick_prompt

    activity = SessionActivity.from_events(candidates[0].ref.session_id, events)
    git = BoundedGit(max_bytes=MAX_RESULT_BYTES)
    drafts: list[dict[str, Any]] = []
    refused: list[tuple[EventRef, EvidenceIncomplete]] = []
    for candidate in candidates:
        anchor = candidate.ref
        try:
            pairs = harvest_pairs(activity, anchor, repo=repo, git_runner=git)
        except EvidenceIncomplete as exc:
            refused.append((anchor, exc))
            continue
        if not pairs or (turn := activity.turn_of(anchor)) is None:
            continue
        choices: list[dict[str, Any]] = []
        for index, pair in enumerate(pairs, 1):
            if (row := lower_pair(activity, anchor, pair, source="captain-hook")) is None:
                raise ValueError("harvested correction pair lost its pinned anchor")
            if repo is not None:
                row = replace(row, detail={"repo": str(repo)})
            choices.append(
                {
                    "pair_id": str(index),
                    "overlap": pair.overlap,
                    "correction_json": json.dumps(asdict(row), ensure_ascii=False, separators=(",", ":")),
                }
            )
        drafts.append(
            {
                "anchor": asdict(anchor),
                "prompt": build_pick_prompt(candidate.text, pairs, anchor_turn=turn.index),
                "choices": choices,
            }
        )
    return drafts, refused


async def jev_pick(prompt: str, count: int) -> str | None:
    """Jev's pick of the candidate a correction prompt's feedback faults, as its number or ``none``.

    ``None`` when Jev timed out, failed, or refused, so the LLM picks instead.
    """
    from spawnllm import JEV, DecideError, DecideKeyMissing, Label, LabelAnswer, decide

    options = dict.fromkeys([UNFAULTED, *map(str, range(1, count + 1))])
    try:
        decision = await decide(prompt, {"candidate": Label(CORRECTION_PICK, options)}, provider=JEV)
    except (DecideError, DecideKeyMissing, TimeoutError, ValueError):
        logger.opt(exception=True).warning("jev gave no correction pick; asking the llm")
        return None
    match decision.answers["candidate"]:
        case LabelAnswer(choice=choice):
            return choice
        case _:
            return None


async def record_correction_drafts(drafts: Sequence[Mapping[str, Any]], *, ledger: CorrectionLedger) -> None:
    from cc_transcript.extract.correct import CorrectionPick, usable_backend
    from spawnllm import extract

    if not drafts:
        return
    backend = usable_backend()
    log = await ledger.handle()
    for draft in drafts:
        anchor = draft["anchor"]
        if await log.for_anchor(anchor["session_id"], anchor["event_uuid"]):
            continue
        choices = draft["choices"]
        if not choices:
            continue
        if (picked := await jev_pick(draft["prompt"], len(choices))) == UNFAULTED:
            continue
        if picked is not None:
            chosen = choices[int(picked) - 1]
        elif backend is None:
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
    windows: Sequence[ContextWindow],
    *,
    source_paths: Sequence[str | None],
    roots: Sequence[str],
    render: Mapping[str, Any],
) -> list[RenderedEvidence | None | Exception]:
    from captain_hook.snapshots.client import NATIVE_CLASSIFIER, EvidenceIncomplete, Lease, SnapshotProtocolError

    if not windows:
        return []
    rendered: list[RenderedEvidence | None | Exception] = [
        SnapshotProtocolError("window was not prepared") for _ in windows
    ]
    grouped: dict[tuple[str, str | None], list[tuple[int, ContextWindow]]] = {}
    for index, (window, path) in enumerate(zip(windows, source_paths, strict=True)):
        grouped.setdefault((window.anchor.session_id, path), []).append((index, window))
    with review_client() as client:
        for (session_id, path), batch in grouped.items():
            leases: list[Lease] = []
            try:
                if path is not None:
                    leases.append(client.acquire(path, classifier=NATIVE_CLASSIFIER).lease)
                else:
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
                    rendered[index] = None if path is not None and exc.status == "missing" else exc
            finally:
                for lease in leases:
                    lease.release()
    return rendered
