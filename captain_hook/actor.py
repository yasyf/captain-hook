from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from spawnllm import LlmBackend, Provider

CREDENTIAL_ENV = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "TYPESAFE_API_KEY",
)
KEY_SOURCES = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY",),
    "typesafe": ("TYPESAFE_API_KEY",),
}
DECIDE_KEYS = {"jev": "typesafe", "openai": "codex"}
KEY_TARGETS = {"codex": "CODEX_API_KEY", "claude": "ANTHROPIC_API_KEY"}
DEFAULT_MODELS = {"codex": "gpt-6.1-sol:xhigh", "claude": "claude-sonnet-5-5:xhigh"}
TIERS = frozenset({"small", "medium", "large"})
CLAUDE_ALIASES = frozenset({"haiku", "sonnet", "opus"})
INFERENCE_REAP_SECONDS = 3.0

ACTOR: ActorJudge | None = None


class JudgeFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActorJudge:
    provider: str
    keys: dict[str, str] = field(repr=False)

    def route(self, explicit: LlmBackend | None, model: str) -> tuple[LlmBackend, str, dict[str, str] | None]:
        from spawnllm import BackendUnavailable, ClaudeCliBackend, CodexCliBackend, OpenAiEndpointBackend

        if isinstance(explicit, OpenAiEndpointBackend):
            return explicit, model, None
        if explicit is not None and type(explicit) not in (CodexCliBackend, ClaudeCliBackend):
            raise BackendUnavailable(f"the {explicit.provider} judge backend is not supported for an API actor")
        provider = explicit.provider if explicit is not None else self.judged_provider(model)
        backend = explicit or (CodexCliBackend() if provider == "codex" else ClaudeCliBackend())
        if provider not in self.keys:
            needed = " or ".join(KEY_SOURCES[provider])
            raise BackendUnavailable(f"the actor holds no {needed} for its {provider} judge")
        if not shutil.which(backend.binary_path()):
            raise BackendUnavailable(f"the actor's {provider} judge CLI {backend.binary} is not installed")
        resolved = backend.resolve_model(model) if model else DEFAULT_MODELS[provider]
        if provider == "claude" and ":" in resolved:
            raise BackendUnavailable(f"the Claude judge cannot run {resolved!r}: spawnllm passes Claude no effort")
        return backend, resolved, {KEY_TARGETS[provider]: self.keys[provider]}

    def decide_key(self, provider: Provider) -> str | None:
        return self.keys.get(DECIDE_KEYS[provider.name])

    def judged_provider(self, model: str | None) -> str:
        from spawnllm import BackendUnavailable

        if not model:
            return self.provider
        if model in TIERS or model.startswith("gpt-"):
            return "codex"
        if model in CLAUDE_ALIASES or model.startswith("claude-"):
            return "claude"
        raise BackendUnavailable(f"the judge model {model!r} names no provider an API actor can run")


def failure_status(exc: Exception) -> str:
    from pydantic import ValidationError
    from spawnllm import BackendCallError

    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, BackendCallError):
        return "provider-error"
    if isinstance(exc, ValidationError):
        return "invalid-output"
    return "failed"


def judged[T](call: Callable[[], T], backend: LlmBackend) -> T:
    try:
        return call()
    except Exception as exc:
        failure = JudgeFailure(f"the {backend.provider} judge failed: {type(exc).__name__} ({failure_status(exc)})")
    raise failure


def single_attempt(attempts: int | None) -> int:
    if attempts not in (None, 1):
        raise ValueError(f"an API actor's judge runs exactly one attempt, not {attempts}")
    return 1


def inference_timeout(timeout: int) -> int:
    from captain_hook.util import reqenv

    if (usable := int(reqenv.seconds_left() - INFERENCE_REAP_SECONDS)) < 1:
        raise TimeoutError("the API actor's event budget leaves no room for the judge and its cleanup")
    return min(timeout, usable)


def capture(provider: str) -> ActorJudge:
    global ACTOR
    found = {name: value for name in CREDENTIAL_ENV if (value := os.environ.pop(name, None))}
    keys = {
        name: value
        for name, sources in KEY_SOURCES.items()
        if (value := next((found[source] for source in sources if source in found), None))
    }
    if provider not in keys:
        raise SystemExit(f"capt-hook: the {provider} actor's environment holds no {' or '.join(KEY_SOURCES[provider])}")
    ACTOR = ActorJudge(provider, keys)
    return ACTOR
