"""Read-only calls on the local Orca runtime's socket, so a hook reads a record without spawning Orca's Electron CLI."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from captain_hook.util import reqenv

RUNTIME_FILE = "orca-runtime.json"
PROTOCOL_VERSION = 3
CONTRACT_VERSION = 1
CONTRACT_CAPABILITY = "orchestration.contract.v1"
REMOTE_SELECTORS = ("ORCA_PAIRING_CODE", "ORCA_REMOTE_PAIRING", "ORCA_ENVIRONMENT")

ADMITTED: dict[str, bool] = {}


class OrcaUnavailable(Exception):
    """The local socket cannot answer this call, so Orca's CLI has to."""


def read_call(argv: tuple[str, ...]) -> tuple[str, dict[str, Any]] | None:
    """The socket method and params for a read-only CLI spelling, or ``None`` when only the CLI serves it."""
    match argv:
        case ("orca", "orchestration", "check", "--terminal", terminal, "--peek", "--json"):
            return "orchestration.check", {
                "terminal": terminal,
                "peek": True,
                "unread": False,
                "compatibilityCliCommand": "orca",
            }
        case ("orca", "orchestration", "worker-show", "--dispatch", dispatch, "--json"):
            return "orchestration.workerShow", {"dispatch": dispatch}
        case ("orca", "orchestration", "run-show", "--id", run, "--json"):
            return "orchestration.runShow", {"id": run}
        case ("orca", "orchestration", "worker-list", "--limit", limit, "--cursor", cursor, "--json"):
            return "orchestration.workerList", {"paginate": True, "limit": int(limit), "cursor": cursor}
        case ("orca", "orchestration", "worker-list", "--limit", limit, "--json"):
            return "orchestration.workerList", {"paginate": True, "limit": int(limit)}
        case ("orca", "terminal", "show", "--terminal", terminal, "--json"):
            return "terminal.show", {"terminal": terminal}
    return None


def call(method: str, params: dict[str, Any], *, timeout: float) -> Any:
    """The result of *method* on the local runtime, in the shape the CLI prints under ``--full``.

    Raises:
        OrcaUnavailable: No local runtime answers, its protocol is not one this client speaks, or it refused the call.
        TimeoutError: The runtime took longer than *timeout* seconds.
    """
    if any(reqenv.getenv(name) for name in REMOTE_SELECTORS):
        raise OrcaUnavailable("a paired runtime is selected")
    deadline = time.monotonic() + timeout
    try:
        runtime = json.loads((user_data_path() / RUNTIME_FILE).read_text())
        endpoint = next(transport["endpoint"] for transport in runtime["transports"] if transport["kind"] == "unix")
        token, runtime_id = runtime["authToken"], runtime["runtimeId"]
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise OrcaUnavailable(f"no local runtime: {exc}") from exc
    if not admitted(runtime_id, endpoint, token, deadline):
        raise OrcaUnavailable(f"runtime {runtime_id} speaks another protocol")
    return exchange(endpoint, token, method, params, deadline)


def user_data_path() -> Path:
    if path := reqenv.getenv("ORCA_USER_DATA_PATH"):
        return Path(path)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "orca"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "orca"


def admitted(runtime_id: str, endpoint: str, token: str, deadline: float) -> bool:
    if (known := ADMITTED.get(runtime_id)) is not None:
        return known
    status = exchange(endpoint, token, "status.get", None, deadline)
    ADMITTED[runtime_id] = (
        CONTRACT_CAPABILITY in status.get("capabilities", ())
        and status.get("minCompatibleRuntimeClientVersion", PROTOCOL_VERSION + 1) <= PROTOCOL_VERSION
        and status.get("runtimeProtocolVersion", 0) >= PROTOCOL_VERSION
    )
    return ADMITTED[runtime_id]


def exchange(endpoint: str, token: str, method: str, params: dict[str, Any] | None, deadline: float) -> Any:
    request: dict[str, Any] = {"id": str(uuid.uuid4()), "authToken": token, "method": method}
    if params is not None:
        request["params"] = params
    if method.startswith("orchestration."):
        request["orchestrationContractVersion"] = CONTRACT_VERSION
        request["compatibilityInvocationId"] = str(uuid.uuid4())
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(max(0.0, deadline - time.monotonic()))
            conn.connect(endpoint)
            conn.sendall(json.dumps(request).encode() + b"\n")
            with conn.makefile("rb") as frames:
                while True:
                    conn.settimeout(max(0.0, deadline - time.monotonic()))
                    if not (line := frames.readline()):
                        raise OrcaUnavailable(f"the runtime closed the socket during {method}")
                    if not (reply := json.loads(line)).get("_keepalive"):
                        break
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        raise OrcaUnavailable(f"{method}: {exc}") from exc
    if reply.get("id") != request["id"] or not reply.get("ok"):
        raise OrcaUnavailable(f"{method}: {reply.get('error')}")
    return reply["result"]
