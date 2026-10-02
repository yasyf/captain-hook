from __future__ import annotations

import importlib
import io
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from signal import SIGKILL, SIGTERM
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from cc_transcript.ids import SessionId
from pydantic import ValidationError
from spawnllm import BackendCallError, ClaudeConfig

from captain_hook import app, context, resource_defaults
from captain_hook.dispatch import format_output
from captain_hook.events import ResourcePressureEvent
from captain_hook.procwatch import judge, screen
from captain_hook.procwatch.identity import ProcessIdentity, start_unix
from captain_hook.procwatch.judge import DisposableVerdict, JudgeFacts
from captain_hook.procwatch.ownership import Owned
from captain_hook.procwatch.settings import PerformanceSettings
from captain_hook.procwatch.signal import Outcome, terminate
from captain_hook.procwatch.state import (
    MAX_DELIVERED,
    MAX_PENDING,
    ProcwatchState,
    StateUnavailable,
    pending_notice,
    record_notice,
    record_refusal,
    record_signal,
    take_pending,
)
from captain_hook.session import ensure_session
from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete
from captain_hook.testing.helpers import mock_resource_pressure_event
from captain_hook.transcripts import LazyTranscript, TranscriptPins
from captain_hook.types import Action, Event, HookResult
from captain_hook.util import proc, reqenv
from captain_hook.util.proc import ProcessTable, Unreadable
from captain_hook.worker.runtime import ProductRuntime
from captain_hook.worker.service import WorkerService
from tests.test_pack_sessions import MAC, OWN_SLEEP
from tests.test_proc import row, table
from tests.test_worker_protocol import abandon, frame, responses
from tests.test_worker_runtime import FakeRegistry

RESOURCES = "captain_hook.builtin_packs.performance.hooks.resources"
CLAUDE = MAC.rows[14575]
DEPLOY_SHELL = row(27400, 14575, "/bin/zsh -c ./deploy.sh production", started="2026-09-30T06:30:00")
DEPLOY_CHILD = row(31400, 27400, "node build.js", started="2026-09-30T06:30:01")
WITH_DEPLOY = table(*MAC.rows.values(), DEPLOY_SHELL, DEPLOY_CHILD)
GAINED_TMUX = table(*MAC.rows.values(), row(31338, OWN_SLEEP.pid, "tmux new -d", started="2026-09-30T06:31:00"))
KEY = f"{OWN_SLEEP.pid}:{start_unix(OWN_SLEEP)}"
BACKEND = MagicMock(provider="claude", resolve_model=lambda model: {"small": "claude-haiku-4-5"}.get(model, model))
GO_OWNED = {
    "enabled",
    "sample_interval_seconds",
    "min_runtime_seconds",
    "cpu_fraction",
    "sustain_seconds",
    "disk_bytes_per_second",
    "grace_seconds",
    "escalate_after_seconds",
    "max_tracked_per_session",
    "registry_cap",
}


def payload(stage: str, child: proc.ProcessRow = OWN_SLEEP, **process: Any) -> dict[str, Any]:
    return {
        "hook_event_name": "ResourcePressure",
        "session_id": "s1",
        "stage": stage,
        "claude_pid": CLAUDE.pid,
        "claude_start_unix": start_unix(CLAUDE),
        "process": {
            "pid": child.pid,
            "ppid": child.ppid,
            "pgid": child.pgid,
            "start_unix": start_unix(child),
            "start_usec": 0,
            "comm": child.argv0,
            "argv": child.command.split(),
            "cwd": "/w",
            "runtime_s": 240.0,
            "cpu_fraction": 0.9,
            "disk_bps": 0.0,
            "ancestry": [],
        }
        | process,
        "metrics": {"cpu": True, "disk": False},
    }


def event(tmp_path: Path, stage: str, child: proc.ProcessRow = OWN_SLEEP) -> ResourcePressureEvent:
    return mock_resource_pressure_event(payload(stage, child), session_dir=tmp_path)


def state(evt: ResourcePressureEvent) -> ProcwatchState:
    return evt.ctx.s[ProcwatchState].get(ProcwatchState())


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [key for key in os.environ if key.startswith(resource_defaults.ENV_PREFIX)]:
        monkeypatch.delenv(key)


@pytest.fixture
def snapshot(monkeypatch: pytest.MonkeyPatch) -> dict[str, ProcessTable | None]:
    holder: dict[str, ProcessTable | None] = {"table": MAC}
    monkeypatch.setattr(proc, "process_table", lambda **kw: holder["table"])
    monkeypatch.setattr(os, "getuid", lambda: 501)
    return holder


@pytest.fixture
def resources(isolate_modules: None) -> ModuleType:
    return importlib.import_module(RESOURCES)


@pytest.fixture
def signals(resources: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[tuple[ProcessIdentity, int]]:
    sent: list[tuple[ProcessIdentity, int]] = []

    def terminate(identity: ProcessIdentity, sig: int, *, recheck: Callable[[], Unreadable | None]) -> Outcome:
        if (why := recheck()) is not None:
            return Outcome(False, why.reason.rstrip("."))
        sent.append((identity, sig))
        return Outcome(True, "sent")

    monkeypatch.setattr(resources, "terminate", terminate)
    return sent


def llm(evt: ResourcePressureEvent, monkeypatch: pytest.MonkeyPatch, outcome: bool | Exception) -> list[Any]:
    calls: list[Any] = []

    def call_llm(prompt: Any, **kwargs: Any) -> DisposableVerdict:
        calls.append((prompt, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return DisposableVerdict(disposable=outcome, reasoning="r")

    monkeypatch.setattr(evt.ctx, "call_llm", call_llm)
    return calls


def validation_error() -> ValidationError:
    try:
        DisposableVerdict.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("an empty verdict validated")


class TestSettings:
    def test_go_owned_defaults_equal_the_contract(self) -> None:
        settings = PerformanceSettings()
        assert {key: getattr(settings, key) for key in GO_OWNED} == resource_defaults.DEFAULTS
        assert set(resource_defaults.DEFAULTS) == GO_OWNED

    def test_python_only_defaults(self) -> None:
        settings = PerformanceSettings()
        assert (settings.terminate, settings.judge_tier, settings.judge_timeout_seconds) == (True, "small", 20)
        assert settings.max_judge_calls_per_session == 10

    def test_env_overrides_use_the_contract_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_PERFORMANCE_TERMINATE", "false")
        monkeypatch.setenv("HOOKS_PERFORMANCE_GRACE_SECONDS", "5")
        settings = PerformanceSettings()
        assert (settings.terminate, settings.grace_seconds) == (False, 5)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            pytest.param("CPU_FRACTION", "0", id="cpu-zero"),
            pytest.param("CPU_FRACTION", "1.5", id="cpu-over-one"),
            pytest.param("CPU_FRACTION", "nan", id="cpu-nan"),
            pytest.param("CPU_FRACTION", "inf", id="cpu-inf"),
            pytest.param("GRACE_SECONDS", "0", id="grace-zero"),
            pytest.param("SUSTAIN_SECONDS", "86401", id="sustain-over-a-day"),
            pytest.param("SAMPLE_INTERVAL_SECONDS", "-1", id="interval-negative"),
            pytest.param("ESCALATE_AFTER_SECONDS", "-1", id="escalate-negative"),
            pytest.param("DISK_BYTES_PER_SECOND", "-1", id="disk-negative"),
            pytest.param("MAX_TRACKED_PER_SESSION", "0", id="tracked-zero"),
            pytest.param("REGISTRY_CAP", "0", id="registry-zero"),
            pytest.param("JUDGE_TIMEOUT_SECONDS", "301", id="judge-timeout-high"),
            pytest.param("MAX_JUDGE_CALLS_PER_SESSION", "0", id="judge-calls-zero"),
        ],
    )
    def test_out_of_domain_values_raise(self, monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
        monkeypatch.setenv(f"HOOKS_PERFORMANCE_{key}", value)
        with pytest.raises(ValidationError):
            PerformanceSettings()

    def test_escalation_may_be_turned_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_PERFORMANCE_ESCALATE_AFTER_SECONDS", "0")
        assert PerformanceSettings().escalate_after_seconds == 0


class TestScreen:
    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            pytest.param("./deploy.sh production", "runs a deploy, release, or publish script", id="deploy-script"),
            pytest.param("/bin/zsh -c ./scripts/release.py", "runs a deploy, release, or publish script", id="shell-c"),
            pytest.param("make deploy", "runs a deploy, release, or publish task", id="make-deploy"),
            pytest.param("npm run release", "runs a deploy, release, or publish task", id="npm-run-release"),
            pytest.param("terraform -chdir=infra apply -auto-approve", "applies infrastructure changes", id="tf"),
            pytest.param("pulumi up --yes", "applies infrastructure changes", id="pulumi"),
            pytest.param("kubectl rollout restart deploy/api", "changes a Kubernetes cluster", id="kubectl"),
            pytest.param("helm upgrade api ./chart", "changes a Helm release", id="helm"),
            pytest.param("gh release create v1.2.0", "cuts a GitHub release", id="gh-release"),
            pytest.param("pnpm publish --access public", "publishes a package", id="pnpm"),
            pytest.param("twine upload dist/*", "publishes a package", id="twine"),
            pytest.param("cargo publish", "publishes a package", id="cargo"),
            pytest.param("uv publish", "publishes a package", id="uv-publish"),
            pytest.param("docker push ghcr.io/x/y:latest", "pushes a container image", id="docker"),
            pytest.param("flyctl deploy --remote-only", "deploys to a cloud provider", id="fly"),
            pytest.param("vercel --prod", "deploys to a cloud provider", id="vercel"),
            pytest.param("aws cloudformation deploy --stack x", "deploys to a cloud provider", id="aws"),
            pytest.param("git -C repo push origin main", "pushes to a git remote", id="git-push"),
            pytest.param("gt submit --stack", "submits a Graphite stack", id="gt"),
            pytest.param("ccx vcs ship -m msg", "ships a version-control change", id="ccx-ship"),
            pytest.param("PGPASSWORD=x psql -h db", "talks to a database", id="psql"),
            pytest.param("mongosh mongodb://db", "talks to a database", id="mongosh"),
            pytest.param("alembic upgrade head", "migrates a database", id="alembic"),
            pytest.param("prisma migrate deploy", "migrates a database", id="prisma"),
            pytest.param("bin/rails db:migrate", "migrates a database", id="rails"),
            pytest.param("sudo launchctl kickstart -k gui/501/x", "changes a system service", id="launchctl"),
            pytest.param("brew upgrade", "changes installed software", id="brew"),
            pytest.param("pip install -e .", "changes installed packages", id="pip"),
            pytest.param("uv sync --extra dev", "changes installed packages", id="uv-sync"),
            pytest.param("cd x && git push", "pushes to a git remote", id="second-segment"),
            pytest.param("python deploy.py", "runs a deploy, release, or migration script", id="python-deploy"),
            pytest.param(
                "python3.12 scripts/release_prod.py", "runs a deploy, release, or migration script", id="python-release"
            ),
            pytest.param("bash ./release.sh", "runs a deploy, release, or migration script", id="bash-script"),
            pytest.param("node tools/migrate.js", "runs a deploy, release, or migration script", id="node-migrate"),
            pytest.param("ruby provision.rb", "runs a deploy, release, or migration script", id="ruby-provision"),
            pytest.param(
                "uv run python scripts/rollout.py", "runs a deploy, release, or migration script", id="uv-run-script"
            ),
            pytest.param("npm run deploy", "runs a deploy, release, or publish task", id="npm-run-deploy"),
            pytest.param("npm run deploy:prod", "runs a deploy, release, or publish task", id="npm-run-deploy-prod"),
            pytest.param("yarn release", "runs a deploy, release, or publish task", id="yarn-release"),
            pytest.param("pnpm run publish-docs", "runs a deploy, release, or publish task", id="pnpm-publish-docs"),
            pytest.param("make -C infra deploy", "runs a deploy, release, or publish task", id="make-C"),
            pytest.param("npx prisma migrate deploy", "migrates a database", id="npx-prisma"),
            pytest.param("pnpm dlx alembic upgrade head", "migrates a database", id="pnpm-dlx"),
            pytest.param("bunx wrangler deploy", "deploys to a cloud provider", id="bunx-wrangler"),
            pytest.param("./scripts/deploy-prod", "runs a deploy, release, or publish script", id="deploy-path"),
            pytest.param(
                "python -W ignore /w/scripts/deploy.py",
                "runs a deploy, release, or migration script",
                id="python-option-value",
            ),
            pytest.param(
                "node --require preload.js /w/scripts/migrate.js",
                "runs a deploy, release, or migration script",
                id="node-option-value",
            ),
            pytest.param(
                "kubectl --context production apply -f manifest.yaml",
                "changes a Kubernetes cluster",
                id="kubectl-global-option",
            ),
            pytest.param(
                "helm --kube-context production upgrade api ./chart", "changes a Helm release", id="helm-global-option"
            ),
            pytest.param(
                "python -Z /w/scripts/build.py", "passes an option the monitor cannot classify", id="python-unknown"
            ),
            pytest.param("node --require", "passes an option the monitor cannot classify", id="node-missing-value"),
            pytest.param(
                "node -e require('./x')", "runs inline interpreter code the monitor cannot classify", id="node-inline"
            ),
            pytest.param("python -m twine upload dist/*", "publishes a package", id="python-module"),
            pytest.param("deno run -A deploy.ts", "runs a deploy, release, or publish script", id="deno-run"),
            pytest.param("uv run --with x pytest -q", None, id="uv-run-valued-option"),
            pytest.param("uv run --no-sync pytest -q", None, id="uv-run-switch"),
            pytest.param("npx --bogus prisma migrate deploy", "passes an option the monitor cannot classify", id="npx"),
            pytest.param("python -Werror -u -B worker.py", None, id="python-switches"),
            pytest.param("python -m pytest tests", None, id="python-pytest"),
            pytest.param("node build.js", None, id="node-build"),
            pytest.param("bash -lc 'cargo test'", None, id="bash-lc-test"),
            pytest.param("rg -n deploy src", None, id="search-for-deploy"),
            pytest.param("rg -n TODO src", None, id="search"),
            pytest.param("uv run pytest tests -q", None, id="tests"),
            pytest.param("cargo build --release", None, id="cargo-build"),
            pytest.param("git status", None, id="git-status"),
            pytest.param("npm run test", None, id="npm-test"),
            pytest.param("/bin/zsh -c source snapshot.sh && eval 'sleep 60'", None, id="claude-shell"),
        ],
    )
    def test_excluded(self, command: str, reason: str | None) -> None:
        assert screen.excluded([command]) == reason

    def test_excluded_screens_ancestors_after_the_child(self) -> None:
        assert screen.excluded(["node build.js", "/bin/zsh -c ./deploy.sh prod"]) == (
            "runs a deploy, release, or publish script"
        )
        assert screen.excluded(["git push", "./deploy.sh"]) == "pushes to a git remote"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("GITHUB_TOKEN=ghp_abc make test", "GITHUB_TOKEN=*** make test", id="token-assignment"),
            pytest.param("env DB_PASSWORD=hunter2 run", "env DB_PASSWORD=*** run", id="password-assignment"),
            pytest.param("x --token abc123 --verbose", "x --token *** --verbose", id="token-flag"),
            pytest.param("x --password=hunter2", "x --password=***", id="password-flag-eq"),
            pytest.param("mysql -p hunter2 db", "mysql -p *** db", id="short-p"),
            pytest.param(
                "curl -H Authorization: Bearer abc.def", "curl -H Authorization: Bearer ***", id="authorization"
            ),
            pytest.param("curl -H bearer xyz", "curl -H bearer ***", id="bearer"),
            pytest.param("git clone https://u:pw@host/r.git", "git clone https://***@host/r.git", id="userinfo"),
            pytest.param("sha " + "a" * 40, "sha ***", id="hex-run"),
            pytest.param("key Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg==", "key ***", id="base64-run"),
            pytest.param("rg -n TODO src/captain_hook", "rg -n TODO src/captain_hook", id="plain"),
            pytest.param("TOKEN=abc run", "TOKEN=*** run", id="exact-token-name"),
            pytest.param("GH_PAT=abc run", "GH_PAT=*** run", id="pat-word"),
            pytest.param("AWS_PROFILE=prod aws s3 ls", "AWS_PROFILE=*** aws s3 ls", id="aws-prefix"),
            pytest.param("stripe_api_key=sk run", "stripe_api_key=*** run", id="lowercase-key"),
            pytest.param("PATH=/usr/bin run", "PATH=/usr/bin run", id="path-kept"),
            pytest.param("x --api-key abc", "x --api-key ***", id="api-key-spaced"),
            pytest.param("x --secret abc", "x --secret ***", id="secret-spaced"),
            pytest.param("x --github-token=abc", "x --github-token=***", id="named-token-flag"),
            pytest.param("docker login --password-stdin user", "docker login --password-stdin user", id="stdin-kept"),
            pytest.param("Bearer xyz", "Bearer ***", id="bare-bearer"),
            pytest.param("deploy --password 'alpha beta' x", "deploy --password *** x", id="quoted-flag-value"),
            pytest.param("env 'TOKEN=alpha beta' run", "env 'TOKEN=***' run", id="quoted-assignment"),
            pytest.param('x TOKEN="alpha beta" y', "x TOKEN=*** y", id="quoted-assignment-value"),
            pytest.param(
                "curl -H 'Authorization: Bearer abc def' u", "curl -H 'Authorization: Bearer ***' u", id="quoted-header"
            ),
        ],
    )
    def test_redact(self, text: str, expected: str) -> None:
        assert screen.redact(text) == expected

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(["analysis", "--password", "alpha beta"], "analysis --password ***", id="flag-value"),
            pytest.param(["TOKEN=alpha beta"], "TOKEN=***", id="assignment"),
            pytest.param(["x", "--api-key=a b"], "x --api-key=***", id="flag-assignment"),
            pytest.param(
                ["curl", "-H", "Authorization: Bearer abc def"], "curl -H Authorization: Bearer ***", id="header"
            ),
            pytest.param(["x", "--token", "--verbose"], "x --token --verbose", id="flag-then-flag"),
            pytest.param(
                ["docker", "login", "--password-stdin", "user"], "docker login --password-stdin user", id="stdin"
            ),
            pytest.param(["git", "clone", "https://u:p w@host/r"], "git clone https://***@host/r", id="userinfo"),
            pytest.param(["PATH=/usr/bin", "rg", "-n", "TODO src"], "PATH=/usr/bin rg -n TODO src", id="plain"),
        ],
    )
    def test_redact_argv(self, argv: list[str], expected: str) -> None:
        assert screen.redact_argv(argv) == expected

    def test_redact_shortens_home(self) -> None:
        assert screen.redact(f"rg x {Path.home()}/Code") == "rg x ~/Code"


class NoExchange:
    def __getattr__(self, name: str) -> object:
        pytest.fail(f"the judge reached the snapshot client: {name}")


class TestJudge:
    def facts(self, evt: ResourcePressureEvent) -> tuple[ProcessIdentity, JudgeFacts]:
        identity = ProcessIdentity.from_payload(evt.process)
        owned = Owned(OWN_SLEEP, CLAUDE, (MAC.rows[27200], CLAUDE))
        return identity, JudgeFacts.of(evt, identity, owned)

    def test_disposable_verdict_is_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        evt = event(tmp_path, "judge")
        calls = llm(evt, monkeypatch, True)
        identity, facts = self.facts(evt)
        assert judge.disposable(evt, PerformanceSettings(), identity, facts) is True
        assert judge.disposable(evt, PerformanceSettings(), identity, facts) is True
        assert len(calls) == 1
        assert calls[0][1] == {
            "model": "small",
            "timeout": 20,
            "response_model": DisposableVerdict,
            "attempts": 1,
            "tools": (),
        }
        assert (state(evt).verdicts, state(evt).judge_calls, state(evt).judging) == ({identity.key: True}, 1, [])

    def test_prompt_carries_redacted_bounded_facts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        evt = mock_resource_pressure_event(
            payload("judge", argv=["deploy-tool", "--token", "s3cret", *["x"] * 400]), session_dir=tmp_path
        )
        calls = llm(evt, monkeypatch, False)
        identity, facts = self.facts(evt)
        judge.disposable(evt, PerformanceSettings(), identity, facts)
        rendered = str(calls[0][0])
        assert "s3cret" not in rendered
        assert "--token ***" in rendered
        assert "untrusted data" in rendered
        assert len(facts.render()) == judge.MAX_CONTEXT_CHARS

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(False, id="reject"),
            pytest.param(TimeoutError("slow"), id="timeout"),
            pytest.param(BackendCallError("down"), id="backend"),
            pytest.param(validation_error(), id="invalid"),
            pytest.param(subprocess.TimeoutExpired("claude", 20), id="subprocess"),
            pytest.param(OSError("spawn"), id="oserror"),
            pytest.param(EvidenceIncomplete("partial", "owner lost"), id="evidence"),
            pytest.param(RuntimeError("preparation group is closed"), id="runtime"),
        ],
    )
    def test_failures_are_not_disposable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool | Exception, logcap: Any
    ) -> None:
        evt = event(tmp_path, "judge")
        llm(evt, monkeypatch, failure)
        identity, facts = self.facts(evt)
        assert judge.disposable(evt, PerformanceSettings(), identity, facts) is False
        assert state(evt).verdicts == {identity.key: False}
        failed = [record.message for record in logcap.records if "judge call failed" in record.message]
        if isinstance(failure, Exception):
            (message,) = failed
            assert f"error={type(failure).__name__!r}" in message
            assert str(failure) in message
        else:
            assert failed == []

    def test_judge_resolves_no_transcript_and_records_no_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt = event(tmp_path, "judge")
        evt.ctx.transcript = LazyTranscript(
            TranscriptPins(lambda: pytest.fail("the judge resolved the session transcript")), seed=True
        )
        prepared: list[str] = []
        monkeypatch.setattr(context.HookContext, "release_preparation", lambda self, prompt: prepared.append(prompt))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(context, "ready_backend", lambda specialty, model: BACKEND)
        parsed = DisposableVerdict(disposable=True, reasoning="r")
        monkeypatch.setattr(
            "spawnllm.run_sync",
            lambda spec, *, backend: SimpleNamespace(error=None, result=SimpleNamespace(parsed=parsed)),
        )
        identity, facts = self.facts(evt)
        token = CURRENT_CLIENT.set(NoExchange())
        try:
            assert judge.disposable(evt, PerformanceSettings(), identity, facts) is True
        finally:
            CURRENT_CLIENT.reset(token)
        assert prepared == []
        assert evt.ctx.prepared_evidence is None

    def test_judge_calls_are_capped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_PERFORMANCE_MAX_JUDGE_CALLS_PER_SESSION", "2")
        verdicts = []
        calls: list[Any] = []
        for start in range(3):
            evt = mock_resource_pressure_event(
                payload("judge", start_unix=start_unix(OWN_SLEEP) + start), session_dir=tmp_path
            )
            recorded = llm(evt, monkeypatch, True)
            identity, facts = self.facts(evt)
            verdicts.append(judge.disposable(evt, PerformanceSettings(), identity, facts))
            calls = [*calls, *recorded]
        assert verdicts == [True, True, Unreadable("this session spent its judge budget.")]
        assert len(calls) == 2
        assert state(evt).judge_calls == 2
        assert len(state(evt).verdicts) == 2

    def test_in_flight_identity_refuses_a_second_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        evt = event(tmp_path, "judge")
        identity, facts = self.facts(evt)
        evt.ctx.s[ProcwatchState].set(ProcwatchState(judging=[identity.key], judge_calls=1))
        calls = llm(evt, monkeypatch, True)
        assert judge.disposable(evt, PerformanceSettings(), identity, facts) == Unreadable(
            "its disposability verdict is already in flight."
        )
        assert calls == []

    def test_corrupt_state_never_grants_a_fresh_budget(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        evt = event(tmp_path, "judge")
        evt.ctx.s[ProcwatchState].path.write_text("{not json")
        calls = llm(evt, monkeypatch, True)
        identity, facts = self.facts(evt)
        with pytest.raises(StateUnavailable, match="corrupt"):
            judge.disposable(evt, PerformanceSettings(), identity, facts)
        assert calls == []

    def test_prompt_masks_a_credential_value_with_spaces_whole(self, tmp_path: Path) -> None:
        evt = mock_resource_pressure_event(
            payload("judge", argv=["deploy-tool", "--password", "alpha beta", "TOKEN=gamma delta"]),
            session_dir=tmp_path,
        )
        identity, facts = self.facts(evt)
        assert "beta" not in facts.render()
        assert "delta" not in facts.render()
        assert "--password *** TOKEN=***" in facts.render()

    def test_judge_makes_one_provider_attempt_with_no_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt = event(tmp_path, "judge")
        specs: list[Any] = []

        def run_sync(spec: Any, *, backend: Any) -> Any:
            specs.append((spec, backend))
            return SimpleNamespace(
                error=None, result=SimpleNamespace(parsed=DisposableVerdict(disposable=True, reasoning="r"))
            )

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("spawnllm.run_sync", run_sync)
        monkeypatch.setattr(context, "ready_backend", lambda specialty, model: BACKEND)
        identity, facts = self.facts(evt)
        assert judge.disposable(evt, PerformanceSettings(), identity, facts) is True
        ((spec, backend),) = specs
        assert backend is BACKEND
        assert (spec.max_attempts, spec.agent, spec.timeout, spec.model) == (1, False, 20, "claude-haiku-4-5")
        assert spec.config_for(ClaudeConfig) == ClaudeConfig(tools=())

    def test_selection_that_spends_the_budget_makes_no_provider_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt = event(tmp_path, "judge")
        clock = [1_000.0]
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: clock[0]))

        def slow_selection(specialty: object, model: object) -> MagicMock:
            clock[0] += 30.0
            return BACKEND

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(context, "ready_backend", slow_selection)
        monkeypatch.setattr("spawnllm.run_sync", lambda *args, **kwargs: pytest.fail("the provider was called"))
        identity, facts = self.facts(evt)
        overrides = reqenv.RequestOverrides(
            env={}, cwd=str(tmp_path), client_ppid=1, session_id="s1", deadline_unix_ms=1_035_000
        )
        with reqenv.use_request(overrides):
            assert judge.disposable(evt, PerformanceSettings(), identity, facts) is False
        assert state(evt).verdicts == {identity.key: False}

    def test_a_verdict_after_abandonment_is_cached_but_never_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt = event(tmp_path, "judge")
        flag = threading.Event()

        def call_llm(prompt: Any, **kwargs: Any) -> DisposableVerdict:
            flag.set()
            return DisposableVerdict(disposable=True, reasoning="r")

        monkeypatch.setattr(evt.ctx, "call_llm", call_llm)
        identity, facts = self.facts(evt)
        with reqenv.abandonable(flag), pytest.raises(reqenv.Abandoned):
            judge.disposable(evt, PerformanceSettings(), identity, facts)
        assert (state(evt).verdicts, state(evt).judging, state(evt).judge_calls) == ({identity.key: True}, [], 1)

    def test_abandonment_before_the_call_releases_the_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evt = event(tmp_path, "judge")
        flag = threading.Event()
        flag.set()
        calls: list[Any] = []

        def call_llm(prompt: Any, **kwargs: Any) -> DisposableVerdict:
            reqenv.checkpoint()
            calls.append(prompt)
            return DisposableVerdict(disposable=True, reasoning="r")

        monkeypatch.setattr(evt.ctx, "call_llm", call_llm)
        identity, facts = self.facts(evt)
        with reqenv.abandonable(flag), pytest.raises(reqenv.Abandoned):
            judge.disposable(evt, PerformanceSettings(), identity, facts)
        assert calls == []
        assert (state(evt).verdicts, state(evt).judging, state(evt).judge_calls) == ({}, [], 0)

    def test_model_sees_redacted_program_and_parents(self, tmp_path: Path) -> None:
        evt = mock_resource_pressure_event(payload("judge", argv=["TOKEN=abc", "x"]), session_dir=tmp_path)
        identity = ProcessIdentity.from_payload(evt.process)
        parent = row(27200, 14575, "GH_PAT=abc sh", started="2026-09-30T06:29:59")
        facts = JudgeFacts.of(evt, identity, Owned(OWN_SLEEP, CLAUDE, (parent, CLAUDE)))
        assert "abc" not in facts.render()


class TestPressureHandler:
    def run(self, resources: ModuleType, evt: ResourcePressureEvent) -> HookResult:
        return resources.pressure(evt)

    def test_warn_records_a_notice_and_acks(
        self, tmp_path: Path, resources: ModuleType, snapshot: dict[str, ProcessTable | None]
    ) -> None:
        evt = event(tmp_path, "warn")
        result = self.run(resources, evt)
        assert result.action is Action.warn
        assert result.message is not None and "`sleep` (pid 31337) has run 4 min at 90% CPU" in result.message
        assert [notice.text for notice in state(evt).pending] == [result.message]
        assert format_output(Event.ResourcePressure, result) == {"decision": "proceed"}

    def test_judge_sends_one_sigterm_after_a_disposable_verdict(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        evt = event(tmp_path, "judge")
        llm(evt, monkeypatch, True)
        result = self.run(resources, evt)
        assert [(identity.pid, sig) for identity, sig in signals] == [(OWN_SLEEP.pid, SIGTERM)]
        assert result.action is Action.warn
        assert [(record.identity, record.signal) for record in state(evt).signals] == [(KEY, SIGTERM)]
        assert (state(evt).verdicts, state(evt).judging) == ({}, [])
        assert "sent SIGTERM to `sleep` (pid 31337)" in state(evt).pending[-1].text

    def test_recheck_refuses_a_target_that_gained_a_protected_child(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        evt = event(tmp_path, "judge")

        def call_llm(prompt: Any, **kwargs: Any) -> DisposableVerdict:
            snapshot["table"] = GAINED_TMUX
            return DisposableVerdict(disposable=True, reasoning="r")

        monkeypatch.setattr(evt.ctx, "call_llm", call_llm)
        result = self.run(resources, evt)
        assert (result.action, signals, state(evt).signals) == (Action.block, [], [])
        assert "terminal multiplexer" in state(evt).pending[-1].text

    def test_second_judge_after_sigterm_sends_nothing(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        evt = event(tmp_path, "judge")
        record_signal(evt, KEY, SIGTERM, "sent")
        calls = llm(evt, monkeypatch, True)
        assert self.run(resources, evt).action is Action.block
        assert (calls, signals) == ([], [])

    def test_invalid_settings_refuse(
        self, tmp_path: Path, resources: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOOKS_PERFORMANCE_CPU_FRACTION", "2")
        result = self.run(resources, event(tmp_path, "warn"))
        assert result.action is Action.block
        assert result.message is not None and "settings are invalid" in result.message

    def test_corrupt_state_refuses_the_stage(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        evt = event(tmp_path, "judge")
        evt.ctx.s[ProcwatchState].path.write_text("[]")
        calls = llm(evt, monkeypatch, True)
        result = self.run(resources, evt)
        assert result.action is Action.block
        assert result.message is not None and "session state is corrupt" in result.message
        assert (calls, signals) == ([], [])

    def test_missing_state_directory_refuses(
        self, resources: ModuleType, snapshot: dict[str, ProcessTable | None]
    ) -> None:
        result = self.run(resources, mock_resource_pressure_event(payload("warn")))
        assert result.action is Action.block
        assert result.message is not None and "no state directory" in result.message

    def test_judge_rejection_sends_nothing(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        evt = event(tmp_path, "judge")
        llm(evt, monkeypatch, False)
        result = self.run(resources, evt)
        assert signals == []
        assert result.action is Action.block
        assert format_output(Event.ResourcePressure, result) is None
        assert state(evt).signals == []
        assert "did not rate it disposable" in state(evt).pending[-1].text

    def test_terminate_off_skips_the_judge(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOOKS_PERFORMANCE_TERMINATE", "false")
        evt = event(tmp_path, "judge")
        calls = llm(evt, monkeypatch, True)
        assert self.run(resources, evt).action is Action.block
        assert (calls, signals) == ([], [])

    @pytest.mark.parametrize("stage", ["warn", "judge"])
    def test_deploy_ancestor_is_excluded_before_the_judge(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
        monkeypatch: pytest.MonkeyPatch,
        stage: str,
    ) -> None:
        snapshot["table"] = WITH_DEPLOY
        evt = event(tmp_path, stage, DEPLOY_CHILD)
        calls = llm(evt, monkeypatch, True)
        result = self.run(resources, evt)
        assert result.action is Action.block
        assert result.message is not None and "deploy, release, or publish script" in result.message
        assert (calls, signals) == ([], [])

    def test_unreadable_table_refuses(
        self, tmp_path: Path, resources: ModuleType, snapshot: dict[str, ProcessTable | None]
    ) -> None:
        snapshot["table"] = None
        result = self.run(resources, event(tmp_path, "warn"))
        assert result.action is Action.block
        assert result.message is not None and "process table is unreadable" in result.message

    def test_disabled_refuses_without_a_notice(
        self, tmp_path: Path, resources: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOOKS_PERFORMANCE_ENABLED", "false")
        evt = event(tmp_path, "warn")
        assert self.run(resources, evt).action is Action.block
        assert state(evt).pending == []

    def test_escalate_without_a_prior_sigterm_sends_nothing(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
    ) -> None:
        evt = event(tmp_path, "escalate")
        assert self.run(resources, evt).action is Action.block
        assert signals == []

    def test_escalate_after_sigterm_sends_sigkill(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
    ) -> None:
        evt = event(tmp_path, "escalate")
        record_signal(evt, KEY, SIGTERM, "sent")
        assert self.run(resources, evt).action is Action.warn
        assert [(identity.pid, sig) for identity, sig in signals] == [(OWN_SLEEP.pid, SIGKILL)]

    def test_escalate_ignores_a_sigterm_to_another_identity(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        signals: list[tuple[ProcessIdentity, int]],
    ) -> None:
        evt = event(tmp_path, "escalate")
        record_signal(evt, f"{OWN_SLEEP.pid}:{start_unix(OWN_SLEEP) - 1}", SIGTERM, "sent")
        assert self.run(resources, evt).action is Action.block
        assert signals == []

    def test_failed_signal_is_reported_as_a_refusal(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            resources, "terminate", lambda identity, sig, recheck: Outcome(False, "exited before the signal")
        )
        evt = event(tmp_path, "judge")
        llm(evt, monkeypatch, True)
        result = self.run(resources, evt)
        assert result.action is Action.block
        assert state(evt).signals == []
        assert state(evt).pending[-1].text.endswith("exited before the signal.")


def pressure_frame(request_id: int, root: Path, env: dict[str, str]) -> dict[str, object]:
    return {
        "protocol": 1,
        "op": "event",
        "id": request_id,
        "request": {
            "schema": 1,
            "event": "ResourcePressure",
            "root": str(root),
            "cwd": str(root),
            "env": env,
            "payload_raw": json.dumps(payload("judge")),
            "client_pid": 100,
            "client_ppid": 99,
            "deadline_unix_ms": int((time.time() + 30) * 1000),
        },
    }


class GatedInput(io.RawIOBase):
    def __init__(self, *chunks: tuple[bytes, threading.Event | None]) -> None:
        self.chunks = list(chunks)
        self.buffer = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        while not self.buffer and self.chunks:
            payload, gate = self.chunks.pop(0)
            if gate is not None:
                assert gate.wait(timeout=5)
            self.buffer = payload
        served, self.buffer = self.buffer[:size], self.buffer[size:]
        return served


class TestAbandonTransport:
    @pytest.mark.parametrize("stall", ["judge", "recheck"])
    def test_the_host_abandon_frame_unwinds_the_hook_before_the_signal(
        self,
        tmp_path: Path,
        resources: ModuleType,
        snapshot: dict[str, ProcessTable | None],
        monkeypatch: pytest.MonkeyPatch,
        stall: str,
    ) -> None:
        reached = threading.Event()
        tables = itertools.count()

        def stalled() -> None:
            reached.set()
            assert reqenv.abandon_signal().wait(timeout=5)

        def process_table(**kwargs: Any) -> ProcessTable:
            if stall == "recheck" and next(tables) == 1:
                stalled()
            return MAC

        def call_llm(self: Any, prompt: Any, **kwargs: Any) -> DisposableVerdict:
            if stall == "judge":
                stalled()
            return DisposableVerdict(disposable=True, reasoning="r")

        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(proc, "process_table", process_table)
        monkeypatch.setattr(context.HookContext, "call_llm", call_llm)
        monkeypatch.setattr(resources, "announce", lambda title, body: None)
        monkeypatch.setattr(
            resources,
            "terminate",
            partial(
                terminate,
                kill=lambda pid, sig: kills.append((pid, sig)),
                usage_row=lambda pid: OWN_SLEEP,
                process_cwd=lambda pid: "/w",
            ),
        )
        hooks = [hook for hook in app.current_state().hooks if hook.handler is resources.pressure]
        runtime = ProductRuntime(
            registry_factory=lambda _: FakeRegistry(app.State(hooks=hooks)),
            install_writer=False,
            nlp_warmer=lambda: None,
        )
        env = {key: value for key, value in os.environ.items() if reqenv.is_whitelisted(key)}
        env.pop("CAPT_HOOK_TEST_NO_LIVE", None)
        output = io.BytesIO()

        WorkerService(
            GatedInput((frame(pressure_frame(1, tmp_path, env)), None), (frame(abandon(1)), reached)),
            output,
            dispatch=runtime.dispatch,
        ).run()

        assert kills == []
        (received,) = [message for message in responses(output.getvalue()) if message["op"] == "result"]
        assert received["id"] == 1
        assert "proceed" not in json.dumps(received)
        evt = mock_resource_pressure_event(payload("judge"), session_dir=ensure_session(SessionId("s1")))
        assert state(evt).signals == []
        assert (state(evt).verdicts, state(evt).judging) == ({KEY: True}, [])


class TestDelivery:
    def test_second_delivery_is_silent(self, tmp_path: Path) -> None:
        evt = event(tmp_path, "warn")
        assert pending_notice(evt) is False
        record_notice(evt, "one")
        assert pending_notice(evt) is True
        assert [notice.text for notice in take_pending(evt)] == ["one"]
        assert pending_notice(evt) is False
        assert take_pending(evt) == []

    def test_notices_are_redacted_when_recorded(self, tmp_path: Path) -> None:
        evt = event(tmp_path, "warn")
        record_notice(evt, "pid 1 exec'd `deploy --token s3cret` in ~/x")
        assert state(evt).pending[-1].text == "pid 1 exec'd `deploy --token ***` in ~/x"

    def test_state_growth_is_bounded(self, tmp_path: Path) -> None:
        evt = event(tmp_path, "warn")
        for index in range(MAX_PENDING + 5):
            record_notice(evt, f"notice {index}")
        assert [notice.text for notice in state(evt).pending][0] == "notice 5"
        assert len(state(evt).pending) == MAX_PENDING
        for _ in range(4):
            for index in range(MAX_PENDING):
                record_notice(evt, f"later {index}")
            take_pending(evt)
        assert len(state(evt).delivered) == MAX_DELIVERED

    def test_refusal_forgets_the_identity(self, tmp_path: Path) -> None:
        evt = event(tmp_path, "judge")
        evt.ctx.s[ProcwatchState].set(ProcwatchState(verdicts={KEY: True, "1:1": False}, judging=[KEY]))
        record_refusal(evt, KEY, "left running")
        assert (state(evt).verdicts, state(evt).judging) == ({"1:1": False}, [])

    def test_deliver_renders_context_and_system_message(self, tmp_path: Path, resources: ModuleType) -> None:
        evt = event(tmp_path, "warn")
        record_notice(evt, "one")
        record_notice(evt, "two")
        result = resources.deliver(evt)
        assert (result.action, result.message, result.system_message) == (Action.warn, "one\n\ntwo", "one\n\ntwo")
        assert resources.deliver(evt) is None


class TestFormatOutput:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            pytest.param(Action.warn, {"decision": "proceed"}, id="warn"),
            pytest.param(Action.allow, {"decision": "proceed"}, id="allow"),
            pytest.param(Action.block, None, id="block"),
        ],
    )
    def test_only_warn_or_allow_acks(self, action: Action, expected: dict[str, str] | None) -> None:
        assert format_output(Event.ResourcePressure, HookResult(action=action, message="m")) == expected
