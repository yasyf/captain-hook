from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from captain_hook import actor
from captain_hook.app import on
from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.types import Event

if TYPE_CHECKING:
    from collections.abc import Iterator

SENTINEL = "sk-synthetic-judge-key-41c2"
OTHER = "sk-synthetic-judge-key-9be0"
PROVIDER_TEXT = "synthetic-provider-text-c0d9"
LS = '{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"ls"}}'
PKILL = '{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"pkill -x sleep"}}'
JUDGE_SKIP = (
    "capt-hook: the mandatory hook {name} did not complete "
    "(JudgeFailure: the codex judge failed: BackendCallError (provider-error)) "
    "and did not check this call. Run `capt-hook logs` to see why."
)

FAKE_CLI = """#!{python}
import json, os, signal, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_CLI_MODE", "ok")
with open({log!r}, "a") as log:
    log.write(json.dumps({{
        "argv": args,
        "codex_key": os.environ.get("CODEX_API_KEY"),
        "anthropic_key": os.environ.get("ANTHROPIC_API_KEY"),
        "openai_key": "OPENAI_API_KEY" in os.environ,
    }}) + "\\n")
if args[:1] in (["login"], ["auth"]):
    sys.exit(1)
if mode == "fail":
    print("{text} out " + str(os.environ.get("CODEX_API_KEY")))
    sys.stderr.write('ERROR: {{"status":503,"text":"{text} err"}}\\n')
    sys.exit(1)
if mode == "stall":
    open({log!r} + ".pid", "w").write(str(os.getpid()))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
sys.stdin.read()
result = '{{"name": "n", "value": 1}}' if "--output-schema" in args else "ok"
open(args[args.index("-o") + 1], "w").write(result)
"""

CONCURRENT_ACTOR = """
import os, sys
from unittest.mock import MagicMock
from captain_hook import actor
from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.util import reqenv
actor.capture("codex")
scope = reqenv.RequestOverrides(env={}, cwd=os.getcwd(), client_ppid=1, session_id="s")
with reqenv.use_request(scope), reqenv.deadline_in(60):
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    print(ctx.call_llm("judge", model="small"))
"""


class Verdict(BaseModel):
    name: str
    value: int


@pytest.fixture(autouse=True)
def clean_actor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in actor.CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(actor, "ACTOR", None)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))


def fake_clis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> Path:
    bin_dir, log = tmp_path / "bin", tmp_path / "cli.jsonl"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text(FAKE_CLI.format(python=sys.executable, log=str(log), text=PROVIDER_TEXT))
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def invocations(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_capture_moves_every_credential_out_of_the_evaluator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OTHER)
    judge = actor.capture("codex")
    assert judge is actor.ACTOR
    assert judge.keys == {"codex": SENTINEL}
    assert not {name for name in actor.CREDENTIAL_ENV if name in os.environ}
    assert SENTINEL not in repr(judge)


def test_capture_refuses_an_actor_without_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY or CODEX_API_KEY"):
        actor.capture("codex")


@pytest.mark.parametrize(
    ("provider", "keys", "model", "backend", "resolved"),
    [
        ("codex", {"OPENAI_API_KEY": SENTINEL}, None, "codex", "gpt-6.1-sol:xhigh"),
        ("codex", {"OPENAI_API_KEY": SENTINEL}, "small", "codex", "gpt-6-luna:low"),
        ("codex", {"OPENAI_API_KEY": SENTINEL}, "gpt-5.5", "codex", "gpt-5.5"),
        ("claude", {"ANTHROPIC_API_KEY": SENTINEL, "OPENAI_API_KEY": OTHER}, "small", "codex", "gpt-6-luna:low"),
        ("claude", {"ANTHROPIC_API_KEY": SENTINEL}, "claude-opus-5-5", "claude", "claude-opus-5-5"),
        ("codex", {"OPENAI_API_KEY": SENTINEL, "ANTHROPIC_API_KEY": OTHER}, "sonnet", "claude", "sonnet"),
    ],
)
def test_route_keeps_explicit_models_and_defaults_only_the_unset_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    keys: dict[str, str],
    model: str | None,
    backend: str,
    resolved: str,
) -> None:
    fake_clis(tmp_path, monkeypatch, "codex", "claude")
    for name, value in keys.items():
        monkeypatch.setenv(name, value)
    judge = actor.capture(provider)
    serving, judged, env = judge.route(None, model)
    target = actor.KEY_TARGETS[backend]
    assert (serving.provider, judged, env) == (backend, resolved, {target: judge.keys[backend]})


def test_route_refuses_what_an_api_actor_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from spawnllm import BackendUnavailable, ClaudeSdkBackend

    fake_clis(tmp_path, monkeypatch, "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    judge = actor.capture("claude")
    with pytest.raises(BackendUnavailable, match="OPENAI_API_KEY or CODEX_API_KEY"):
        judge.route(None, "small")
    with pytest.raises(BackendUnavailable, match="names no provider"):
        judge.route(None, "gemini-3-pro")
    with pytest.raises(BackendUnavailable, match="not supported for an API actor"):
        judge.route(ClaudeSdkBackend(), "small")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(BackendUnavailable, match="CLI claude is not installed"):
        judge.route(None, None)


def test_route_leaves_an_explicit_endpoint_judge_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from spawnllm import OpenAiEndpointBackend

    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    endpoint = OpenAiEndpointBackend("https://example.invalid/v1", "m", api_key="synthetic")
    assert actor.capture("codex").route(endpoint, "small") == (endpoint, "small", None)


def test_resident_readiness_probes_a_login_the_api_actor_never_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spawnllm import BackendNotAuthenticated, CodexCliBackend

    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    assert isinstance(CodexCliBackend().check_status(), BackendNotAuthenticated)
    assert [entry["argv"][:2] for entry in invocations(log)] == [["login", "status"]]


@pytest.mark.parametrize(
    "call",
    [
        {},
        {"response_model": Verdict},
        {"tools": ()},
    ],
    ids=["call", "extract", "run-spec"],
)
def test_every_call_path_runs_the_selected_api_judge_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: dict
) -> None:
    from captain_hook.util import reqenv

    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    scope = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")
    with (
        patch("spawnllm.select_backend", side_effect=AssertionError("API actors never auto-select")),
        reqenv.use_request(scope),
        reqenv.deadline_in(60),
    ):
        result = ctx.call_llm("judge this", model="small", **call)
    assert result == (Verdict(name="n", value=1) if "response_model" in call else "ok")
    [entry] = invocations(log)
    argv = entry["argv"]
    assert argv[:2] == ["exec", "--ephemeral"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv
    assert argv[argv.index("--model") + 1] == "gpt-6-luna"
    assert "model_reasoning_effort=low" in argv
    assert {"--ignore-user-config", "features.hooks=false", "features.mcp_servers=false"} <= set(argv)
    assert (entry["codex_key"], entry["openai_key"], entry["anthropic_key"]) == (SENTINEL, False, None)
    assert SENTINEL not in os.environ.values()


def test_concurrent_actors_keep_their_own_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = []
    children = []
    for name, key in (("one", SENTINEL), ("two", OTHER)):
        root = tmp_path / name
        root.mkdir()
        logs.append(fake_clis(root, monkeypatch, "codex"))
        env = {k: v for k, v in os.environ.items() if k not in actor.CREDENTIAL_ENV}
        path = f"{root / 'bin'}{os.pathsep}{env['PATH']}"
        env |= {"OPENAI_API_KEY": key, "CLAUDE_PROJECT_DIR": str(root), "PATH": path}
        children.append(
            subprocess.Popen([sys.executable, "-c", CONCURRENT_ACTOR], env=env, stdout=subprocess.PIPE, text=True)
        )
    outputs = [child.communicate(timeout=60)[0] for child in children]
    assert [child.returncode for child in children] == [0, 0]
    assert outputs == ["ok\n", "ok\n"]
    assert [[entry["codex_key"] for entry in invocations(log)] for log in logs] == [[SENTINEL], [OTHER]]


def test_a_judge_never_starts_without_room_for_its_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.util import reqenv

    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    scope = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")
    with reqenv.use_request(scope), reqenv.deadline_in(actor.INFERENCE_REAP_SECONDS + 0.5):
        with pytest.raises(TimeoutError, match="no room for the judge and its cleanup"):
            ctx.call_llm("judge this", model="small")
    assert invocations(log) == []
    with reqenv.use_request(scope), reqenv.deadline_in(actor.INFERENCE_REAP_SECONDS + 5.5):
        assert actor.inference_timeout(180) == 5


def judge_scope(tmp_path: Path):
    from captain_hook.util import reqenv

    return reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")


def test_a_failing_judge_surfaces_only_typed_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback

    from loguru import logger

    from captain_hook import faults
    from captain_hook.log import setup_logging
    from captain_hook.util import reqenv

    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "fail")
    monkeypatch.setenv("CAPTAIN_HOOK_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    setup_logging("actor-failure")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    with reqenv.use_request(judge_scope(tmp_path)), reqenv.deadline_in(60):
        with pytest.raises(actor.JudgeFailure) as raised:
            ctx.call_llm("judge this", model="small")
    failure = raised.value
    assert str(failure) == "the codex judge failed: BackendCallError (provider-error)"
    assert failure.__cause__ is None and failure.__context__ is None
    logger.bind(hook="general.synthetic:llm_judge").opt(exception=failure).error("hook handler failed")
    faults.record("hook general.synthetic:llm_judge", failure, str(tmp_path))
    artifacts = [path.read_text() for path in tmp_path.rglob("*.json")]
    drained = faults.drain(str(tmp_path))
    logger.complete()
    surfaces = ["".join(traceback.format_exception(failure)), repr(failure), *drained, *artifacts]
    surfaces += [path.read_text() for path in (tmp_path / "logs").rglob("*.log")]
    assert any("provider-error" in surface for surface in surfaces)
    assert not [surface for surface in surfaces if PROVIDER_TEXT in surface or SENTINEL in surface]
    assert len(invocations(log)) == 1


@contextmanager
def unavailable_endpoint() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Unavailable(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            body = json.dumps({"error": {"message": PROVIDER_TEXT}}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Unavailable)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()


def test_a_failing_endpoint_judge_runs_once_and_surfaces_only_typed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback

    from spawnllm import OpenAiEndpointBackend

    from captain_hook import faults
    from captain_hook.util import reqenv

    monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    with unavailable_endpoint() as (url, requests):
        endpoint = OpenAiEndpointBackend(url, "m", api_key=OTHER)
        with reqenv.use_request(judge_scope(tmp_path)), reqenv.deadline_in(60):
            with pytest.raises(actor.JudgeFailure) as raised:
                ctx.call_llm("judge this", backend=endpoint)
    failure = raised.value
    assert str(failure) == f"the {endpoint.provider} judge failed: BackendCallError (provider-error)"
    assert failure.__cause__ is None and failure.__context__ is None
    faults.record("plain_english rewrite", failure, str(tmp_path))
    surfaces = ["".join(traceback.format_exception(failure)), repr(failure)]
    surfaces += [path.read_text() for path in tmp_path.rglob("*.json")]
    assert not [surface for surface in surfaces if PROVIDER_TEXT in surface or OTHER in surface or SENTINEL in surface]
    assert requests == ["/v1/chat/completions"]


def test_an_api_judge_runs_exactly_one_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.util import reqenv

    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    with reqenv.use_request(judge_scope(tmp_path)), reqenv.deadline_in(60):
        with pytest.raises(ValueError, match="exactly one attempt, not 3"):
            ctx.call_llm("judge this", model="small", attempts=3)
        assert invocations(log) == []
        assert ctx.call_llm("judge this", model="small", attempts=1) == "ok"
    assert len(invocations(log)) == 1


def test_claude_judge_effort_is_refused_before_any_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from spawnllm import BackendUnavailable, ClaudeCliBackend

    log = fake_clis(tmp_path, monkeypatch, "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    judge = actor.capture("claude")
    for model in ("claude-opus-5-5:high", None):
        with pytest.raises(BackendUnavailable, match="passes Claude no effort"):
            judge.route(None, model)
    serving, resolved, _ = judge.route(ClaudeCliBackend(), "claude-opus-5-5")
    assert (serving.provider, resolved) == ("claude", "claude-opus-5-5")
    assert invocations(log) == []


def test_a_stalled_judge_is_reaped_before_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from captain_hook.util import reqenv

    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "stall")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    budget = actor.INFERENCE_REAP_SECONDS + 2.0
    started = time.monotonic()
    with reqenv.use_request(judge_scope(tmp_path)), reqenv.deadline_in(budget):
        with pytest.raises(actor.JudgeFailure, match="timeout"):
            ctx.call_llm("judge this", model="small")
    assert time.monotonic() - started < budget
    pid = int(Path(f"{log}.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def failing_codex_actor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = fake_clis(tmp_path, monkeypatch, "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "fail")
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    return log


def actor_response(root: Path, *, event: str = "PreToolUse", payload: str, mandatory: bool) -> Any:
    import time
    from dataclasses import replace

    from captain_hook import app
    from captain_hook.worker.runtime import ProductRuntime
    from tests.test_pack_sessions import HOOK_SHELL
    from tests.test_worker_runtime import FakeRegistry, request

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(app.current_state()),
        transcript_loader=lambda path: None,
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    deadline = int((time.time() + 60) * 1000)
    response, _ = runtime.dispatch(
        replace(
            request(event=event, payload_raw=payload, mandatory=mandatory),
            root=str(root),
            cwd=str(root),
            env={"CLAUDE_PROJECT_DIR": str(root)},
            client_ppid=HOOK_SHELL,
            deadline_unix_ms=deadline,
        )
    )
    return response


def replied(response: Any) -> dict[str, Any]:
    return json.loads(response.stdout.splitlines()[-1])


def leaks(response: Any) -> list[str]:
    return [text for text in (response.stdout, response.stderr) if PROVIDER_TEXT in text or SENTINEL in text]


@pytest.fixture
def general_guard(isolate_modules: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.loader import discover_pack
    from captain_hook.util import proc
    from tests.test_pack_sessions import MAC, PACKS_DIR

    monkeypatch.setattr(proc, "process_table", lambda **kw: MAC)
    monkeypatch.setattr("captain_hook.heartbeat.record_heartbeat", lambda *args: None)
    discover_pack("general", PACKS_DIR / "general" / "hooks")


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_a_failed_actor_judge_skips_its_mandatory_hook_with_only_typed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    log = failing_codex_actor(tmp_path, monkeypatch)

    @on(Event[event], mandatory=True)
    def judged_policy(evt: Any) -> None:
        evt.ctx.call_llm("judge this call", model="small", evidence=False)

    response = actor_response(tmp_path, event=event, payload=LS, mandatory=False)
    envelope = replied(response)
    assert response.exit == 0
    assert envelope["systemMessage"] == JUDGE_SKIP.format(name="judged_policy")
    assert "permissionDecision" not in response.stdout
    assert "behavior" not in response.stdout
    assert leaks(response) == []
    assert len(invocations(log)) == 1


def test_a_settled_block_stands_beside_a_failed_actor_judge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = failing_codex_actor(tmp_path, monkeypatch)

    @on(Event.PreToolUse, mandatory=True)
    def slack_policy(evt: Any) -> Any:
        return evt.block("permission denied")

    @on(Event.PreToolUse, mandatory=True)
    def judged_audit(evt: Any) -> None:
        evt.ctx.call_llm("judge this call", model="small", evidence=False)

    response = actor_response(tmp_path, payload=LS, mandatory=False)
    envelope = replied(response)
    assert response.exit == 0
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "permission denied" in envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert JUDGE_SKIP.format(name="judged_audit") in envelope["systemMessage"]
    assert leaks(response) == []
    assert len(invocations(log)) == 1


def test_an_actor_completes_the_guard_only_when_every_mandatory_hook_finished(
    general_guard: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_codex_actor(tmp_path, monkeypatch)
    completed = actor_response(tmp_path, payload=PKILL, mandatory=True)
    assert (completed.exit, completed.guard) == (0, "completed")
    assert replied(completed)["hookSpecificOutput"]["permissionDecision"] == "deny"

    @on(Event.PreToolUse, mandatory=True)
    def judged_audit(evt: Any) -> None:
        evt.ctx.call_llm("judge this call", model="small", evidence=False)

    response = actor_response(tmp_path, payload=PKILL, mandatory=True)
    envelope = replied(response)
    assert (response.exit, response.guard) == (0, "")
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert JUDGE_SKIP.format(name="judged_audit") in envelope["systemMessage"]
    assert leaks(response) == []


def test_an_actor_confirm_failure_notes_only_its_type_once_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logcap: Any
) -> None:
    from captain_hook import Confirm
    from captain_hook.dispatch import execute_hook
    from captain_hook.events import PreToolUseEvent
    from captain_hook.types import Action, HookResult, HookSpec, RegisteredHook
    from captain_hook.util import reqenv
    from tests.helpers import build_ctx

    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    actor.capture("codex")
    spec = HookSpec(events=Event.PreToolUse, message="m", block=True, confirm=Confirm(rule="r"))
    guard = RegisteredHook(spec=spec, name="queued_push")
    ctx = build_ctx(session_dir=tmp_path)
    scope = reqenv.RequestOverrides(env={"CEREBRAS_API_KEY": OTHER}, cwd=str(tmp_path), client_ppid=1, session_id="s")
    with unavailable_endpoint() as (url, requests):
        monkeypatch.setattr("captain_hook.confirm.CONFIRM_ENDPOINT", url)
        with reqenv.use_request(scope), reqenv.deadline_in(60):
            results = [
                execute_hook(guard, PreToolUseEvent(_raw={"tool_name": "Bash", "tool_input": {"command": c}}, ctx=ctx))
                for c in ("git push origin feat", "git push origin main")
            ]
    message = "queued_push: allowed, the confirm step failed (JudgeFailure)"
    assert results == [HookResult(action=Action.warn, message=message, approve=False), None]
    assert requests == ["/v1/chat/completions"] * 2
    assert "confirm step failed" in logcap.text
    assert not [secret for secret in (PROVIDER_TEXT, OTHER, SENTINEL) if secret in logcap.text]
