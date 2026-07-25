"""The signed Captain Hook bridge client and notification delivery loop.

Python never connects to the helper socket. It invokes the fixed signed bridge embedded in
``Captain Hook.app``; that bridge owns the exact DaemonKit session, peer authentication, and
typed ping/notify exchange. The deployment-owned LaunchAgents keep the app and host available;
``notify`` never mutates service state and never raises into the review pipeline.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Mapping

APP_PATH = Path.home() / "Applications" / "Captain Hook.app"
INSTALLED_BRIDGE = APP_PATH / "Contents" / "Helpers" / "capt-hook-helper-client"

BRIDGE_TIMEOUT = 7.0
PAYLOAD_CAP = 64 * 1024


class Lane(StrEnum):
    """Which lane delivered or dropped a notification."""

    bridge = "bridge"
    dropped = "dropped"


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """The typed result of a :func:`notify` call."""

    lane: Lane
    ok: bool
    error: str | None


def helper_dir() -> Path:
    """The helper's home, with a test-only override."""
    if override := reqenv.getenv("CAPT_HOOK_HELPER_DIR"):
        return Path(override)
    return Path.home() / ".capt-hook"


def bridge_path() -> Path:
    """The fixed app-embedded signed bridge, with a test-only override."""
    if override := reqenv.getenv("CAPT_HOOK_HELPER_CLIENT"):
        return Path(override)
    return INSTALLED_BRIDGE


def status_path() -> Path:
    """The snapshot the widget reads."""
    return helper_dir() / "status.json"


def encode_payload(payload: Mapping[str, object]) -> bytes:
    """Encode one typed bridge payload without transport framing."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def send(
    operation: str,
    payload: Mapping[str, object] | None = None,
    *,
    timeout: float = BRIDGE_TIMEOUT,
) -> dict[str, object]:
    """Invoke the signed bridge for one typed operation and validate its terminal result."""
    argv = [str(bridge_path()), operation]
    try:
        completed = subprocess.run(
            argv,
            input=encode_payload(payload) if payload is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"bridge failed: {exc}") from exc
    if not completed.stdout:
        detail = completed.stderr.decode(errors="replace").strip() or f"exit {completed.returncode}"
        raise OSError(f"bridge failed: {detail}")
    reply = json.loads(completed.stdout)
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise ValueError(f"malformed bridge reply: {reply!r}")
    expected_exit = 0 if reply["ok"] else 3
    if completed.returncode != expected_exit:
        raise OSError(f"bridge exit {completed.returncode} does not match reply ok={reply['ok']}")
    for field in ("version", "error"):
        if field in reply and reply[field] is not None and not isinstance(reply[field], str):
            raise ValueError(f"malformed bridge {field}: {reply[field]!r}")
    return reply


def notify(
    *,
    title: str,
    kind: str,
    subtitle: str | None = None,
    body: str | None = None,
    url: str | None = None,
    repo: str | None = None,
) -> NotifyOutcome:
    """Deliver once through the deployment-owned signed bridge."""
    payload: dict[str, object] = {"kind": kind, "title": title}
    for field, value in (("subtitle", subtitle), ("body", body), ("url", url), ("repo", repo)):
        if value is not None:
            payload[field] = value
    if len(encode_payload(payload)) > PAYLOAD_CAP:
        logger.warning("capt-hook notify payload exceeds the frame cap — dropped", kind=kind)
        return NotifyOutcome(Lane.dropped, ok=False, error="payload exceeds frame cap")

    if (outcome := _try_bridge(payload)) is not None:
        return outcome
    logger.warning("capt-hook deployed helper is unreachable — notification dropped", kind=kind)
    return NotifyOutcome(Lane.dropped, ok=False, error="helper unreachable; run `capt-hook helper install`")


def _try_bridge(payload: Mapping[str, object]) -> NotifyOutcome | None:
    """Return a bridge result, or ``None`` when the deployment is unavailable."""
    try:
        reply = send("notify", payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return NotifyOutcome(Lane.bridge, ok=reply["ok"], error=_reply_error(reply))


def _reply_error(reply: Mapping[str, object]) -> str | None:
    return str(error) if (error := reply.get("error")) is not None else None
