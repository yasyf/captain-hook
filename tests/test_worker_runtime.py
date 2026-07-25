from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass
from typing import Any

from captain_hook import app
from captain_hook.daemon.context import ContextIO
from captain_hook.util import reqenv
from captain_hook.worker.protocol import EventRequest
from captain_hook.worker.runtime import ProductRuntime


@dataclass(slots=True)
class Snapshot:
    state: app.State
    discovery_stdout: str = "discovered out\n"
    discovery_stderr: str = "discovered err\n"


class FakeRegistry:
    def __init__(self) -> None:
        self.calls = 0

    def get(self) -> Snapshot:
        self.calls += 1
        return Snapshot(app.State())


def request(*, request_id: int = 1, event: str = "PreToolUse", payload_raw: str = "{}") -> EventRequest:
    return EventRequest(
        id=request_id,
        event=event,
        async_=True,
        root="/project",
        cwd="/project/subdir",
        env={"CLAUDE_PROJECT_DIR": "/project"},
        payload_raw=payload_raw,
        python="/usr/bin/python3",
        build="12.9.1",
        client_pid=100,
        client_ppid=99,
    )


def test_dispatch_binds_request_scope_and_replays_cached_discovery() -> None:
    registry = FakeRegistry()
    seen: dict[str, Any] = {}

    def transcript_loader(_: object) -> None:
        return None

    def dispatch(root: object, event: object, raw: object, **kwargs: object) -> dict[str, str]:
        seen.update(
            root=root,
            event=event,
            raw=raw,
            kwargs=kwargs,
            overrides=reqenv.current(),
        )
        return {"decision": "allow"}

    runtime = ProductRuntime(
        registry_factory=lambda _: registry,
        dispatcher=dispatch,
        transcript_loader=transcript_loader,
        install_writer=False,
    )
    response = runtime.dispatch(request())

    assert response.status == "ok"
    assert response.exit == 0
    assert response.stdout == 'discovered out\n{"decision": "allow"}\n'
    assert response.stderr == "discovered err\n"
    assert str(seen["root"]) == "/project"
    assert seen["event"].name == "PreToolUse"
    assert seen["kwargs"] == {
        "session_dir": None,
        "async_": True,
        "transcript_loader": transcript_loader,
    }
    assert seen["overrides"].cwd == "/project/subdir"
    assert seen["overrides"].client_ppid == 99
    assert reqenv.current() is None


def test_registry_is_reused_for_the_same_root() -> None:
    registry = FakeRegistry()
    factories = 0

    def factory(_: object) -> FakeRegistry:
        nonlocal factories
        factories += 1
        return registry

    runtime = ProductRuntime(registry_factory=factory, dispatcher=lambda *_, **__: None, install_writer=False)
    runtime.dispatch(request(request_id=1))
    runtime.dispatch(request(request_id=2))

    assert factories == 1
    assert registry.calls == 2


def test_invalid_event_is_a_result_error_without_dispatch() -> None:
    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=lambda *_, **__: None,
        install_writer=False,
    )
    response = runtime.dispatch(request(event="NoSuchEvent"))

    assert response.status == "ok"
    assert response.exit == 1
    assert "Invalid event type: 'NoSuchEvent'" in response.stderr


def test_dispatch_exception_returns_traceback_error() -> None:
    def fail(*_: object, **__: object) -> None:
        raise ValueError("broken hook")

    runtime = ProductRuntime(registry_factory=lambda _: FakeRegistry(), dispatcher=fail, install_writer=False)
    response = runtime.dispatch(request())

    assert response.status == "error"
    assert response.exit == 1
    assert "ValueError: broken hook" in response.stderr


def test_hook_writes_are_captured_inside_the_product_response() -> None:
    def dispatch(*_: object, **__: object) -> None:
        print("hook stdout")
        print("hook stderr", file=sys.stderr)

    runtime = ProductRuntime(registry_factory=lambda _: FakeRegistry(), dispatcher=dispatch, install_writer=False)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = ContextIO("stdout", io.StringIO())
    sys.stderr = ContextIO("stderr", io.StringIO())
    try:
        response = runtime.dispatch(request())
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    assert response.stdout == "discovered out\nhook stdout\n"
    assert response.stderr == "discovered err\nhook stderr\n"


def test_augment_path_appends_missing_user_bin_dirs(monkeypatch, tmp_path) -> None:
    from captain_hook.worker import __main__ as worker_main

    present = tmp_path / "present"
    present.mkdir()
    already = tmp_path / "already"
    already.mkdir()
    absent = tmp_path / "absent"
    monkeypatch.setattr(worker_main, "PATH_FALLBACKS", (str(present), str(already), str(absent)))
    monkeypatch.setenv("PATH", f"/usr/bin:{already}")

    worker_main.augment_path()

    assert os.environ["PATH"] == f"/usr/bin:{already}:{present}"


def test_augment_path_leaves_complete_path_untouched(monkeypatch, tmp_path) -> None:
    from captain_hook.worker import __main__ as worker_main

    covered = tmp_path / "covered"
    covered.mkdir()
    monkeypatch.setattr(worker_main, "PATH_FALLBACKS", (str(covered),))
    monkeypatch.setenv("PATH", f"/usr/bin:{covered}")

    worker_main.augment_path()

    assert os.environ["PATH"] == f"/usr/bin:{covered}"
