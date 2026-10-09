from __future__ import annotations

import re
import subprocess
import threading
import time
from copy import copy
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from captain_hook.model import Model
from captain_hook.prompt import Prompt
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import RemoteSession
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_project_dir

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cc_transcript.query import Session
    from cc_transcript.render import Budget
    from pydantic import BaseModel
    from spawnllm import DecideError, DecideKeyMissing, Decision, LlmBackend, Provider, TModel, TSpecialty
    from spawnllm.decide import Question, TDecideProvider

    from captain_hook.settings import HooksSettings
    from captain_hook.signals.nlp import Clause
    from captain_hook.transcripts import LazyTranscript
    from captain_hook.turn import Turn


RECENT_WINDOW = 15
UNSUPPORTED_MODELS: dict[tuple[str, str], ModelRejection] = {}
UNSUPPORTED_MODELS_LOCK = threading.Lock()
READY_BACKEND_TTL_SECONDS = 300.0
READY_BACKENDS: dict[tuple[str | None, str], tuple[float, LlmBackend]] = {}
READY_BACKENDS_LOCK = threading.Lock()
DECIDE_MARGIN_SECONDS = 0.5
VERDICT_TIMEOUT_SECONDS = 5.0


def decision_prompt(state: str, instructions: str) -> str:
    return f"{state}\n\n<task>\n{instructions}\n</task>"


def record_decide_failure(
    provider: TDecideProvider, exc: DecideError | DecideKeyMissing, root: str | None
) -> DecideError | DecideKeyMissing:
    from spawnllm import DecideError

    from captain_hook import faults

    match exc:
        case DecideError(status=status):
            failure: DecideError | DecideKeyMissing = DecideError(status, "the provider rejected the request")
        case _:
            failure = exc
    faults.record(f"decide ({provider})", failure, root)
    return failure


@dataclass(frozen=True, slots=True)
class ModelRejection:
    provider: str
    model: str
    message: str


def last_error_line(exc: BaseException) -> str:
    return str(exc).rstrip().rpartition("\n")[2]


def is_unsupported_model(exc: BaseException) -> bool:
    """Whether ``exc`` is a backend's HTTP 400 rejecting the requested model, which no retry can fix.

    Only the message's last line counts: codex echoes the prompt into the stderr the message
    carries, so a prompt quoting such a rejection must not pass for one.
    """
    from spawnllm import BackendCallError

    return isinstance(exc, BackendCallError) and bool(
        re.search(r'(?:^|\s)ERROR: \{.*"status":\s*400\b.*\bmodel\b.*\bnot supported\b', last_error_line(exc))
    )


def ready_backend(specialty: TSpecialty | None, model: TModel | str) -> LlmBackend:
    """The backend spawnllm would select, probed at most once per :data:`READY_BACKEND_TTL_SECONDS`.

    Selection spawns the backend's CLI to check its login, ``claude auth status`` for the default
    one, and a hook that asks an LLM on every prompt paid that spawn on every call.
    """
    from spawnllm import select_backend

    key = (specialty, model)
    with READY_BACKENDS_LOCK:
        if (ready := READY_BACKENDS.get(key)) is not None and time.monotonic() - ready[0] < READY_BACKEND_TTL_SECONDS:
            return ready[1]
        backend = select_backend(specialty=specialty, model=model)
        READY_BACKENDS[key] = (time.monotonic(), backend)
        return backend


def remember_model_rejection(specialty: str, model: str, exc: BaseException, backend: LlmBackend) -> None:
    resolved = backend.resolve_model(model)
    if resolved.partition(":")[0] in last_error_line(exc):
        with UNSUPPORTED_MODELS_LOCK:
            UNSUPPORTED_MODELS[(specialty, model)] = ModelRejection(backend.provider, resolved, str(exc))


def run_spec(
    prompt: str,
    backend: LlmBackend,
    model: TModel,
    response_model: type[BaseModel] | None,
    *,
    agent: bool,
    cwd: str | None,
    timeout: int,
    attempts: int | None,
    tools: tuple[str, ...] | None,
    env: dict[str, str] | None = None,
) -> str | BaseModel:
    from spawnllm import ClaudeConfig, CodexConfig, RunSpec, run_sync

    configs: dict[str, ClaudeConfig | CodexConfig] = {} if tools is None else {"claude": ClaudeConfig(tools=tools)}
    if env is not None and backend.provider == "codex":
        configs["codex"] = CodexConfig(bypass_approvals_and_sandbox=True)
    spec = RunSpec(
        prompt=prompt,
        model=backend.resolve_model(model),
        response_model=response_model,
        agent=agent,
        cwd=cwd,
        env=env,
        api_auth=env is not None,
        timeout=timeout,
        provider_configs=configs,
        **({} if attempts is None else {"max_attempts": attempts}),
    )
    resp = run_sync(spec, backend=backend)
    if resp.error is not None:
        raise resp.error.ex
    return resp.result.raw if response_model is None else resp.result.parsed


def transcript_window(transcript: bool | int | Literal["recent", "full"]) -> int | None:
    match transcript:
        case "full":
            return None
        case True | "recent":
            return RECENT_WINDOW
        case int() as events:
            return events


def render_window(
    transcript: Session | RemoteSession, *, window: int | None, tool_results: bool, budget: Budget | None
) -> str:
    from cc_transcript.render import Budget, render_turn

    src = transcript if window is None else transcript.recent_messages(window)
    if isinstance(src, RemoteSession):
        return src.render(budget=budget or Budget(), tool_results=tool_results)
    return "\n\n".join(
        rendered
        for turn in src.turns
        if (rendered := render_turn(turn, budget=budget or Budget(), tool_results=tool_results))
    )


@dataclass(frozen=True)
class HookEvidence:
    source_ref: str | None
    source_path: Path | None
    event_count: int
    window_start: int
    current_turn_event_count: int
    signal_texts: tuple[tuple[tuple[int | Literal["turn"], str, bool], tuple[str, ...]], ...]
    prompt: str


@dataclass
class HookContext:
    """Runtime context injected into every hook event.

    Holds session state, the transcript ``Session``, settings, and LLM/CLI helpers. Inside a
    subagent or teammate lane, or a Claude session on a live Orca dispatch, ``transcript`` is the
    lane's own and ``root_transcript`` tails the session that spawned or dispatched it, where the
    user's own words live; it is ``None`` everywhere else.
    """

    session: SessionStore
    transcript: Session | RemoteSession | LazyTranscript
    settings: HooksSettings | None
    project_root: Path | None = None
    root_transcript: Session | RemoteSession | LazyTranscript | None = None
    root_path: Path | None = None
    signal_evidence: dict[tuple[int | Literal["turn"], str, bool], tuple[str, ...]] = field(
        default_factory=dict, init=False
    )
    prepared_evidence: HookEvidence | None = field(default=None, init=False)

    def fork(self, transcript: Session | RemoteSession | LazyTranscript) -> HookContext:
        context = copy(self)
        context.transcript = transcript
        context.signal_evidence = {}
        context.prepared_evidence = None
        for name in (
            "event_count",
            "window_start",
            "model",
            "current_turn_event_count",
            "transcript_path",
            "transcript_ref",
            "turn",
            "prior",
        ):
            context.__dict__.pop(name, None)
        return context

    @cached_property
    def event_count(self) -> int:
        return len(self.transcript)

    @cached_property
    def model(self) -> Model | None:
        """The session's model, from the transcript's latest assistant reply; ``None`` before the first."""
        return None if (model := self.t.model) is None else Model.parse(model)

    @cached_property
    def window_start(self) -> int:
        return self.t.window_start if isinstance(self.t, RemoteSession) else 0

    @cached_property
    def current_turn_event_count(self) -> int:
        return len(self.turn)

    @cached_property
    def transcript_path(self) -> Path | None:
        return self.t.path

    @cached_property
    def transcript_ref(self) -> str | None:
        return self.t.evidence_ref if isinstance(self.t, RemoteSession) else None

    def release_preparation(self, prompt: str) -> None:
        from captain_hook.transcripts import release_transcript

        self.prepared_evidence = HookEvidence(
            source_ref=self.transcript_ref,
            source_path=self.transcript_path,
            event_count=self.event_count,
            window_start=self.window_start,
            current_turn_event_count=self.current_turn_event_count,
            signal_texts=tuple(self.signal_evidence.items()),
            prompt=prompt,
        )
        release_transcript(self.transcript)

    @property
    def t(self) -> Session | RemoteSession:
        """Alias for ``transcript``."""
        from captain_hook.transcripts import LazyTranscript

        return self.transcript.resolve() if isinstance(self.transcript, LazyTranscript) else self.transcript

    @property
    def s(self) -> SessionStore:
        """Alias for ``session``."""
        return self.session

    @property
    def state(self) -> SessionStore:
        """Alias for ``session``."""
        return self.session

    @property
    def conf(self) -> HooksSettings | None:
        """Alias for ``settings``."""
        return self.settings

    @property
    def c(self) -> HooksSettings | None:
        """Alias for ``settings`` (shortest form)."""
        return self.conf

    @cached_property
    def turn(self) -> Turn | RemoteSession:
        """The one-turn view of the current turn (cached), with prompt matching via ``matches``."""
        from captain_hook.turn import Turn

        current = self.t.current_turn
        if isinstance(current, RemoteSession):
            return current
        return Turn(current.turns, current.path)

    @cached_property
    def prior(self) -> Session | RemoteSession:
        """The session window before the current turn's last exchange (cached)."""
        return self.t.prior()

    def nlp(self, text: str, *patterns: str | Clause) -> bool:
        """Whether ``text`` matches any pattern — the escape hatch for matching arbitrary prose.

        A string pattern is a case-insensitive regex; a :class:`~captain_hook.Clause`
        runs the dependency-clause scan. For the current turn's prompt, prefer
        ``evt.ctx.turn.matches(*patterns)``.

        Example:
            >>> evt.ctx.nlp(evt.ctx.t.assistant_text(), Clause(noun=Phrase("test"), verb=Phrase("skip")))
        """
        from captain_hook.signals.nlp import scan_text

        return scan_text(text, patterns)

    def transcript_text(
        self, *, window: int | None = None, tool_results: bool = False, budget: Budget | None = None
    ) -> str:
        """The transcript rendered turn by turn under ``budget``.

        Args:
            window: Render only the span covering the most recent ``window`` messages; ``None`` is the whole session.
            tool_results: Render each tool result after its call, as ``result:`` or ``failed:``.
            budget: Character budgets for prose, tool calls and answer previews; ``None`` is cc-transcript's default.
        """
        return render_window(self.t, window=window, tool_results=tool_results, budget=budget)

    def transcript_block(
        self, *, window: int | None = RECENT_WINDOW, tool_results: bool = False, budget: Budget | None = None
    ) -> str:
        rendered = self.transcript_text(window=window, tool_results=tool_results, budget=budget)
        if isinstance(self.t, RemoteSession):
            return f'<transcript evidence="{self.t.evidence_ref}">\n{rendered}\n</transcript>'
        return f"<transcript>\n{rendered}\n</transcript>"

    def root_transcript_block(
        self, *, window: int | None = RECENT_WINDOW, tool_results: bool = False, budget: Budget | None = None
    ) -> str:
        """The lane's root session rendered like ``transcript_block`` as ``<root_transcript>``; empty outside a lane."""
        from captain_hook.transcripts import LazyTranscript

        if self.root_transcript is None:
            return ""
        root = self.root_transcript
        src = root.resolve() if isinstance(root, LazyTranscript) else root
        rendered = render_window(src, window=window, tool_results=tool_results, budget=budget)
        return f"<root_transcript>\n{rendered}\n</root_transcript>"

    def root_excerpt(self, needles: Sequence[str], *, around: int = 2) -> Session | None:
        """The root session's events that mention any of ``needles``, with ``around`` events either side,
        from the last 16 MiB of its transcript rather than its event tail; ``None`` outside a lane.
        """
        from captain_hook.transcripts import root_excerpt

        return None if self.root_path is None else root_excerpt(self.root_path, needles, around=around)

    def root_excerpt_block(
        self, needles: Sequence[str], *, tool_results: bool = False, budget: Budget | None = None
    ) -> str:
        """The root excerpt for ``needles`` rendered as ``<root_excerpt>``; empty outside a lane or with no match."""
        if (excerpt := self.root_excerpt(needles)) is None or not len(excerpt):
            return ""
        rendered = render_window(excerpt, window=None, tool_results=tool_results, budget=budget)
        return f"<root_excerpt>\n{rendered}\n</root_excerpt>"

    def call_cli(
        self,
        args: list[str],
        *,
        input: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
        throw: bool = True,
    ) -> str | None:
        from spawnllm.proc import run_cli

        reqenv.checkpoint()
        try:
            return run_cli(
                args,
                input=input,
                timeout=timeout,
                env=reqenv.env_map() | (env or {}),
                cwd=resolve_project_dir() or reqenv.cwd(),
            )
        except (OSError, subprocess.SubprocessError):
            if throw:
                raise
            return None

    def git(self, *args: str) -> str | None:
        try:
            return self.call_cli(["git", *args], timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def diff(
        self,
        source: str = "uncommitted",
        *,
        commit: str | None = None,
        scope: str | None = None,
        budget: int = 4000,
    ) -> str | None:
        """A compact diff via ``ccx vcs diff`` when available, else plain ``git``.

        Prefers cc-context's token-budgeted ``ccx vcs diff`` and falls back to ``git`` when ``ccx``
        is absent or failing, so a hook gets a real diff in any repo. The git fallback is bounded to
        roughly ``budget`` tokens with a trailing marker when truncated, so a large diff can't blow
        the caller's context.

        Args:
            source: ``"uncommitted"`` (the default), ``"staged"``, or any git ref. Ignored when
                ``commit`` is set.
            commit: When set, the diff *introduced by* this commit (root-commit-safe), overriding
                ``source``; its git fallback is ``git show --stat -p``, so it carries the commit
                header + diffstat ahead of the patch.
            scope: Restrict the diff to this path.
            budget: Token budget for both the ``ccx`` output and the bounded git fallback.
        """
        target = f"{commit}~1..{commit}" if commit is not None else source
        if (out := self.ccx_diff(target, scope=scope, budget=budget)) is not None:
            return out
        git_scope = ("--", scope) if scope else ()
        if commit is not None:
            return self.bounded_git(budget, "show", "--stat", "-p", commit, *git_scope)
        match source:
            case "uncommitted":
                args = ["diff"]
            case "staged":
                args = ["diff", "--staged"]
            case ref:
                args = ["diff", ref]
        return self.bounded_git(budget, *args, *git_scope)

    def ccx_diff(self, target: str, *, scope: str | None, budget: int) -> str | None:
        cmd = ["ccx", "vcs", "diff", target, "--budget", str(budget), *(("--scope", scope) if scope else ())]
        out = self.call_cli(cmd, throw=False)
        return out if out is not None and ("@@" in out or "diff --git" in out) else None

    def bounded_git(self, budget: int, *args: str) -> str | None:
        if (out := self.git(*args)) is None or len(out) <= (limit := budget * 4):
            return out
        return out[:limit].rstrip() + f"\n... [diff truncated to ~{budget} tokens] ..."

    @cached_property
    def changed_paths(self) -> frozenset[Path] | None:
        if (out := self.git("diff", "--name-only", "HEAD", "--no-renames")) is None or (root := self.repo_root) is None:
            return None
        return frozenset((root / line).resolve() for line in out.splitlines() if line)

    @cached_property
    def repo_root(self) -> Path | None:
        if self.project_root is not None:
            return self.project_root.resolve()
        return Path(out.strip()) if (out := self.git("rev-parse", "--show-toplevel")) else None

    @cached_property
    def current_branch(self) -> str | None:
        return out.strip() if (out := self.git("symbolic-ref", "--short", "HEAD")) else None

    @overload
    def call_llm[M: BaseModel](
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "review",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        tool_results: bool = False,
        budget: Budget | None = None,
        diff: bool | str = False,
        agent: bool = False,
        backend: LlmBackend | None = None,
        attempts: int | None = None,
        tools: tuple[str, ...] | None = None,
        evidence: bool = True,
        response_model: type[M],
        **kwargs: Any,
    ) -> M: ...

    @overload
    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "review",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        tool_results: bool = False,
        budget: Budget | None = None,
        diff: bool | str = False,
        agent: bool = False,
        backend: LlmBackend | None = None,
        attempts: int | None = None,
        tools: tuple[str, ...] | None = None,
        evidence: bool = True,
        response_model: None = None,
        **kwargs: Any,
    ) -> str: ...

    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "review",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        tool_results: bool = False,
        budget: Budget | None = None,
        diff: bool | str = False,
        agent: bool = False,
        backend: LlmBackend | None = None,
        attempts: int | None = None,
        tools: tuple[str, ...] | None = None,
        evidence: bool = True,
        response_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> str | BaseModel:
        """Ask the selected backend once the request's deadline still has room, clamping the call's timeout to it.

        ``attempts`` caps the provider attempts spawnllm makes for one call and ``tools`` names the
        built-in tools the model may use (``()`` for none); either one routes the call through a
        :class:`spawnllm.RunSpec` of its own, and ``None`` keeps spawnllm's defaults. A deadline
        already passed once the backend is selected raises ``TimeoutError`` without a provider call.
        ``evidence=False`` records no transcript evidence for the call, so the session transcript is
        never resolved: the lane for a prompt built from no transcript at all, such as a host event's.
        """
        from spawnllm import BackendCallError, call_sync, extract_sync

        from captain_hook import actor

        reqenv.checkpoint()
        if (judge := actor.ACTOR) is not None:
            serving, judged, actor_env = judge.route(backend, model)
        else:
            serving, judged, actor_env = backend or ready_backend(specialty, model), model, None
        reqenv.checkpoint()
        if reqenv.deadline_within(0):
            raise TimeoutError("the caller deadline passed before the model call")
        with UNSUPPORTED_MODELS_LOCK:
            rejection = UNSUPPORTED_MODELS.get((specialty, model))
        if rejection is not None:
            if (serving.provider, serving.resolve_model(model)) == (rejection.provider, rejection.model):
                raise BackendCallError(rejection.message)
            with UNSUPPORTED_MODELS_LOCK:
                UNSUPPORTED_MODELS.pop((specialty, model), None)
        diff_text = self.diff("uncommitted" if diff is True else diff) if diff else None
        prompt = self.assemble_prompt(
            template, args, kwargs, transcript=transcript, tool_results=tool_results, budget=budget, diff_text=diff_text
        )
        if evidence:
            self.release_preparation(prompt)
        cwd = resolve_project_dir()
        if judge is not None:
            once, limit = actor.single_attempt(attempts), actor.inference_timeout(timeout)
            return actor.judged(
                lambda: run_spec(
                    prompt,
                    serving,
                    judged,
                    response_model,
                    agent=agent,
                    cwd=cwd,
                    timeout=limit,
                    attempts=once,
                    tools=tools,
                    env=actor_env,
                ),
                serving,
            )
        timeout = reqenv.clamp_timeout(timeout)
        try:
            if attempts is not None or tools is not None:
                return run_spec(
                    prompt,
                    serving,
                    model,
                    response_model,
                    agent=agent,
                    cwd=cwd,
                    timeout=timeout,
                    attempts=attempts,
                    tools=tools,
                )
            if response_model is not None:
                return extract_sync(
                    prompt,
                    response_model,
                    backend=serving,
                    specialty=specialty,
                    model=model,
                    agent=agent,
                    cwd=cwd,
                    timeout=timeout,
                )
            return call_sync(
                prompt, backend=serving, specialty=specialty, model=model, agent=agent, cwd=cwd, timeout=timeout
            )
        except BackendCallError as exc:
            if is_unsupported_model(exc):
                remember_model_rejection(specialty, model, exc, serving)
            raise

    def decide(
        self,
        state: str | Mapping[str, Any] | Sequence[Any],
        questions: Mapping[str, Question],
        *,
        provider: Provider,
        timeout: float,
    ) -> Decision:
        """Ask a decision provider once the request's deadline still has room, clamping ``timeout`` to it.

        The call ends :data:`DECIDE_MARGIN_SECONDS` before the caller deadline, retries included, and
        raises ``TimeoutError`` without a request once that leaves no time. An API actor passes the
        key it captured; on a host, spawnllm reads the provider's variable or its macOS Keychain item.
        """
        from spawnllm import decide_sync

        from captain_hook import actor

        reqenv.checkpoint()
        if (left := reqenv.seconds_left()) is not None:
            timeout = min(timeout, left - DECIDE_MARGIN_SECONDS)
        if timeout <= 0:
            raise TimeoutError("the caller deadline leaves no time for the decision call")
        key = actor.ACTOR.decide_key(provider) if actor.ACTOR is not None else None
        return decide_sync(state, questions, provider=provider, timeout=timeout, api_key=key)

    def decide_verdict[M: BaseModel](
        self,
        instructions: str,
        state: str,
        response_model: type[M],
        *,
        root: str | None,
        evidence: bool = True,
        defaults: bool = False,
    ) -> M | None:
        """Ask TypeSafe Jev for the categorical fields of ``response_model``, or ``None`` when it refuses one.

        :func:`~captain_hook.primitives.llm.verdict_questions` turns each field into one question
        under ``instructions``, and :func:`~captain_hook.primitives.llm.verdict_from` maps the answers
        back. A rejected request or a missing key records a fault under ``root`` and raises, as a
        timeout does. ``evidence=False`` keeps the transcript open after the call, as in :meth:`call_llm`.
        ``defaults=True`` keeps the default of every field Jev cannot answer, as a two-stage call's first stage.

        Raises:
            TypeError: When a field of ``response_model`` needs free text Jev cannot give.
        """
        from spawnllm import JEV, DecideError, DecideKeyMissing

        from captain_hook.primitives.llm import verdict_from, verdict_questions

        if (questions := verdict_questions(instructions, response_model, defaults=defaults)) is None:
            raise TypeError(f"{response_model.__name__} has a field Jev cannot answer; ask the LLM instead")
        if evidence:
            self.release_preparation(decision_prompt(state, instructions))
        try:
            decision = self.decide(state, questions, provider=JEV, timeout=VERDICT_TIMEOUT_SECONDS)
        except (DecideError, DecideKeyMissing) as exc:
            record_decide_failure("jev", exc, root)
            raise
        return verdict_from(response_model, questions, decision)

    def transcript_evidence(
        self,
        *,
        transcript: bool | int | Literal["recent", "full"],
        tool_results: bool,
        budget: Budget | None = None,
        root_transcript: bool | int | Literal["recent", "full"] = False,
        root_excerpt: Sequence[str] = (),
    ) -> str:
        return "\n\n".join(
            rendered
            for rendered in (
                self.root_excerpt_block(root_excerpt, tool_results=tool_results, budget=budget) if root_excerpt else "",
                self.root_transcript_block(
                    window=transcript_window(root_transcript), tool_results=tool_results, budget=budget
                )
                if root_transcript
                else "",
                self.transcript_block(window=transcript_window(transcript), tool_results=tool_results, budget=budget)
                if transcript
                else "",
            )
            if rendered
        )

    def assemble_prompt(
        self,
        template: str | Prompt,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        transcript: bool | int | Literal["recent", "full"],
        tool_results: bool,
        budget: Budget | None = None,
        diff_text: str | None,
        root_transcript: bool | int | Literal["recent", "full"] = False,
        root_excerpt: Sequence[str] = (),
    ) -> str:
        block = self.transcript_evidence(
            transcript=transcript,
            tool_results=tool_results,
            budget=budget,
            root_transcript=root_transcript,
            root_excerpt=root_excerpt,
        )
        match template:
            case Prompt():
                prompt = str(template.context("diff", diff_text))
                if not block:
                    return prompt
                return f"{block}\n\n<task>\n{prompt}\n</task>"
            case str():
                wrapped = f"{{transcript}}\n\n<task>\n{template}\n</task>" if block else template
                body = wrapped.format(*args, **kwargs, transcript=block)
                return f"<diff>\n{diff_text}\n</diff>\n\n{body}" if diff_text is not None else body
