from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from captain_hook.prompt import Prompt
from captain_hook.session import SessionStore
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_project_dir

if TYPE_CHECKING:
    from cc_transcript.query import Session
    from pydantic import BaseModel
    from spawnllm import LlmBackend, TModel, TSpecialty

    from captain_hook.settings import HooksSettings
    from captain_hook.signals.nlp import Clause
    from captain_hook.turn import Turn


RECENT_WINDOW = 15
UNSUPPORTED_MODEL = re.compile(r'(?:^|\s)ERROR: \{.*"status":\s*400\b.*\bmodel\b.*\bnot supported\b')
UNSUPPORTED_MODELS: dict[tuple[str, str], ModelRejection] = {}
UNSUPPORTED_MODELS_LOCK = threading.Lock()


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

    return isinstance(exc, BackendCallError) and UNSUPPORTED_MODEL.search(last_error_line(exc)) is not None


def remember_model_rejection(specialty: str, model: str, exc: BaseException, backend: LlmBackend | None) -> None:
    from spawnllm import BackendUnavailable, select_backend

    try:
        serving = backend or select_backend(specialty=specialty, model=model)
    except BackendUnavailable:
        return
    resolved = serving.resolve_model(model)
    if resolved.partition(":")[0] in last_error_line(exc):
        with UNSUPPORTED_MODELS_LOCK:
            UNSUPPORTED_MODELS[(specialty, model)] = ModelRejection(serving.provider, resolved, str(exc))


def transcript_window(transcript: bool | int | Literal["recent", "full"]) -> int | None:
    match transcript:
        case "full":
            return None
        case True | "recent":
            return RECENT_WINDOW
        case int() as events:
            return events


@dataclass
class HookContext:
    """Runtime context injected into every hook event.

    Holds session state, the transcript ``Session``, settings, and LLM/CLI helpers.
    """

    session: SessionStore
    transcript: Session
    settings: HooksSettings | None
    project_root: Path | None = None

    @property
    def t(self) -> Session:
        """Alias for ``transcript``."""
        return self.transcript

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
    def turn(self) -> Turn:
        """The one-turn view of the current turn (cached), with prompt matching via ``matches``."""
        from captain_hook.turn import Turn

        return Turn((current := self.transcript.current_turn).turns, current.path)

    @cached_property
    def prior(self) -> Session:
        """The session window before the current turn's last exchange (cached)."""
        return self.transcript.prior()

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

    def transcript_text(self, *, window: int | None = None) -> str:
        """The transcript rendered turn by turn under the default budget.

        Args:
            window: Render only the most recent ``window`` events; ``None`` renders the whole session.
        """
        from cc_transcript.render import Budget, render_turn

        src = self.transcript if window is None else self.transcript.recent(window)
        return "\n\n".join(rendered for turn in src.turns if (rendered := render_turn(turn, budget=Budget())))

    def transcript_block(self, *, window: int | None = RECENT_WINDOW) -> str:
        """The rendered transcript wrapped in a ``<transcript>`` tag carrying its source path.

        Defaults to a recent-event window rather than the whole session. The render clips long
        turns and tool calls under :class:`Budget`, so an agent-mode LLM uses the path to read
        the untruncated content (e.g. a full ``ExitPlanMode`` plan) or earlier history.

        Args:
            window: Render only the most recent ``window`` events; ``None`` renders the whole session.
        """
        rendered = self.transcript_text(window=window)
        if (path := self.transcript.path) is not None:
            return f'<transcript path="{path}">\n{rendered}\n</transcript>'
        return f"<transcript>\n{rendered}\n</transcript>"

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
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        diff: bool | str = False,
        agent: bool = False,
        backend: LlmBackend | None = None,
        response_model: type[M],
        **kwargs: Any,
    ) -> M: ...

    @overload
    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        diff: bool | str = False,
        agent: bool = False,
        backend: LlmBackend | None = None,
        response_model: None = None,
        **kwargs: Any,
    ) -> str: ...

    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        diff: bool | str = False,
        agent: bool = False,
        backend: LlmBackend | None = None,
        response_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> str | BaseModel:
        from spawnllm import BackendCallError, call_sync, extract_sync, select_backend

        reqenv.checkpoint()
        with UNSUPPORTED_MODELS_LOCK:
            rejection = UNSUPPORTED_MODELS.get((specialty, model))
        if rejection is not None:
            serving = backend or select_backend(specialty=specialty, model=model)
            if (serving.provider, serving.resolve_model(model)) == (rejection.provider, rejection.model):
                raise BackendCallError(rejection.message)
            with UNSUPPORTED_MODELS_LOCK:
                UNSUPPORTED_MODELS.pop((specialty, model), None)
        diff_text = self.diff("uncommitted" if diff is True else diff) if diff else None
        prompt = self.assemble_prompt(template, args, kwargs, transcript=transcript, diff_text=diff_text)
        cwd = resolve_project_dir()
        timeout = reqenv.clamp_timeout(timeout)
        try:
            if response_model is not None:
                return extract_sync(
                    prompt,
                    response_model,
                    backend=backend,
                    specialty=specialty,
                    model=model,
                    agent=agent,
                    cwd=cwd,
                    timeout=timeout,
                )
            return call_sync(
                prompt, backend=backend, specialty=specialty, model=model, agent=agent, cwd=cwd, timeout=timeout
            )
        except BackendCallError as exc:
            if is_unsupported_model(exc):
                remember_model_rejection(specialty, model, exc, backend)
            raise

    def assemble_prompt(
        self,
        template: str | Prompt,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        transcript: bool | int | Literal["recent", "full"],
        diff_text: str | None,
    ) -> str:
        window = transcript_window(transcript) if transcript else None
        match template:
            case Prompt():
                prompt = str(template.context("diff", diff_text))
                return f"{self.transcript_block(window=window)}\n\n<task>\n{prompt}\n</task>" if transcript else prompt
            case str():
                block = self.transcript_block(window=window) if transcript else ""
                wrapped = f"{{transcript}}\n\n<task>\n{template}\n</task>" if transcript else template
                body = wrapped.format(*args, **kwargs, transcript=block)
                return f"<diff>\n{diff_text}\n</diff>\n\n{body}" if diff_text is not None else body
