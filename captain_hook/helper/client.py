"""The frozen ``~/.capt-hook/helper.sock`` protocol client — the single notify seam.

The desktop helper (``Captain Hook.app``) serves a newline-delimited JSON request/response
socket, one request per connection, LF-terminated, protocol version ``v`` 1 — the framing
conventions of :mod:`captain_hook.daemon.protocol`. This module is the ``capt-hook`` client:
it encodes a request (:func:`send`), and :func:`notify` drives the full connect → launch →
poll loop the review pipeline fires a notification through, modeled on
:mod:`capt_hook_client.client`'s ``exchange``/connect-then-spawn shape.

The paths module lives here too — :func:`helper_dir`, :func:`socket_path`, :func:`status_path`
are what every other helper-facing module imports. ``CAPT_HOOK_HELPER_DIR`` overrides the
location for tests only; the sandboxed widget appex always reads the literal ``~/.capt-hook``.

:func:`notify` never raises into its caller: it catches only ``OSError`` (connect/timeout),
``subprocess.SubprocessError`` (the ``open`` launch), and ``json.JSONDecodeError`` (a garbled
reply), and returns a typed :class:`NotifyOutcome` — a dropped notification is logged, never
an exception on the review hot path. There is no ``osascript`` fallback: the only lanes are
the socket and a logged drop.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Mapping

PROTOCOL = 1
APP_NAME = "Captain Hook"

SOCKET_TIMEOUT = 2.0
OPEN_TIMEOUT = 5.0
LAUNCH_POLL_INTERVAL = 0.25
LAUNCH_POLL_BUDGET = 3.0
RECV_CHUNK = 65536
LINE_CAP = 65536


class Lane(StrEnum):
    """Which lane delivered (or dropped) a notification.

    ``socket`` — the helper accepted the request over ``helper.sock``. ``dropped`` — the helper
    was unreachable and could not be launched (or kept refusing), so the notification was logged
    and discarded. The draft's ``osascript`` lane is removed: an unreachable helper drops.
    """

    socket = "socket"
    dropped = "dropped"


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """The typed result of a :func:`notify` — which lane ran, whether it succeeded, and why not.

    Attributes:
        lane: The lane that handled the request (:attr:`Lane.socket` or :attr:`Lane.dropped`).
        ok: Whether the helper acknowledged the notification (``ok:true`` on the wire).
        error: The failure reason — the helper's ``error`` field, or a local drop reason — else ``None``.
    """

    lane: Lane
    ok: bool
    error: str | None


def helper_dir() -> Path:
    """The helper's home — ``~/.capt-hook`` unless ``CAPT_HOOK_HELPER_DIR`` overrides it (tests only)."""
    if override := reqenv.getenv("CAPT_HOOK_HELPER_DIR"):
        return Path(override)
    return Path.home() / ".capt-hook"


def socket_path() -> Path:
    """The helper control socket, ``<helper_dir>/helper.sock``."""
    return helper_dir() / "helper.sock"


def status_path() -> Path:
    """The snapshot the widget reads, ``<helper_dir>/status.json``."""
    return helper_dir() / "status.json"


def encode(request: Mapping[str, object]) -> bytes:
    """Encodes one request to its exact wire bytes: compact JSON, declared key order, LF-terminated."""
    return (json.dumps(request, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def send(request: Mapping[str, object], *, timeout: float = SOCKET_TIMEOUT) -> dict[str, object]:
    """Sends one request to the helper and returns its decoded reply — one connection, one exchange.

    Raises:
        OSError: The socket is unreachable, or the deadline expired mid-exchange.
        json.JSONDecodeError: The helper's reply was not valid JSON.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path()))
        sock.sendall(encode(request))
        line = _read_line(sock)
    finally:
        sock.close()
    return json.loads(line)


def _read_line(sock: socket.socket) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        if not (chunk := sock.recv(RECV_CHUNK)):
            raise OSError("helper closed the connection before responding")
        buf.extend(chunk)
        if len(buf) > LINE_CAP:
            raise OSError("helper response exceeded the line cap")
    return bytes(buf)


def notify(
    *,
    title: str,
    kind: str,
    subtitle: str | None = None,
    body: str | None = None,
    url: str | None = None,
    repo: str | None = None,
) -> NotifyOutcome:
    """Fires one notification through the helper, launching it if needed — never raises.

    Tries the socket; on failure launches ``Captain Hook.app`` with ``open -g`` and polls the
    socket for up to :data:`LAUNCH_POLL_BUDGET` seconds; a helper that stays unreachable (or was
    never installed, so ``open`` fails fast) drops with a logged warning.

    Args:
        title: The banner title (required).
        kind: The notification kind (``pr_open``/``pr_merged``/``review_failure``).
        subtitle: The banner subtitle, omitted from the wire when ``None``.
        body: The banner body, omitted when ``None``.
        url: The click-through URL (http/https), omitted when ``None``.
        repo: The repo key threading related notifications, omitted when ``None``.
    """
    request: dict[str, object] = {"v": PROTOCOL, "op": "notify", "kind": kind, "title": title}
    for field, value in (("subtitle", subtitle), ("body", body), ("url", url), ("repo", repo)):
        if value is not None:
            request[field] = value
    if len(encode(request)) > LINE_CAP:
        logger.warning("capt-hook notify request exceeds the line cap — dropped", kind=kind)
        return NotifyOutcome(Lane.dropped, ok=False, error="request exceeds line cap")

    if (outcome := _try_socket(request)) is not None:
        return outcome
    if not _launch():
        logger.info("capt-hook helper not reachable and not launchable — notification dropped", kind=kind)
        return NotifyOutcome(Lane.dropped, ok=False, error="helper not installed")
    deadline = time.monotonic() + LAUNCH_POLL_BUDGET
    while time.monotonic() < deadline:
        time.sleep(LAUNCH_POLL_INTERVAL)
        if (outcome := _try_socket(request)) is not None:
            return outcome
    logger.warning("capt-hook helper did not answer after launch — notification dropped", kind=kind)
    return NotifyOutcome(Lane.dropped, ok=False, error="helper unreachable after launch")


def _try_socket(request: Mapping[str, object]) -> NotifyOutcome | None:
    """Returns the socket-lane outcome, or ``None`` when the helper is unreachable (relaunch worth trying)."""
    try:
        reply = send(request)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(reply, dict):
        return NotifyOutcome(Lane.socket, ok=False, error=f"malformed reply: {reply!r}")
    if reply.get("v") != PROTOCOL:
        return NotifyOutcome(Lane.socket, ok=False, error=f"protocol mismatch: helper v={reply.get('v')!r}")
    return NotifyOutcome(Lane.socket, ok=bool(reply.get("ok")), error=_reply_error(reply))


def _reply_error(reply: Mapping[str, object]) -> str | None:
    return str(err) if (err := reply.get("error")) is not None else None


def _launch() -> bool:
    """Launches the helper in the background; returns ``False`` when ``open`` fails (helper not installed)."""
    try:
        subprocess.run(
            ["open", "-g", "-a", APP_NAME],
            check=True,
            capture_output=True,
            timeout=OPEN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
