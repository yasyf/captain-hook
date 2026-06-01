"""LLM CLI backends for :func:`HookContext.call_llm`.

Each backend maps the framework's abstract :data:`TModel` sizes to provider
model names and knows how to build the CLI invocation and parse its response.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel

TSpecialty = Literal["debugging", "review", "general"]
TModel = Literal["small", "medium", "large"]


class LlmBackend(ABC):
    """Abstract interface for an LLM CLI backend.

    Concrete backends map abstract :data:`TModel` sizes to provider-specific
    model names and encapsulate how to invoke the provider's CLI and parse the
    raw response.

    Attributes:
        models: Mapping from abstract model size to the provider's model name.
    """

    models: ClassVar[dict[TModel, str]]

    @abstractmethod
    def build_command(self, model: str, schema_path: str | None, agent: bool) -> list[str]:
        """Build the CLI argv for a single LLM invocation.

        Args:
            model: Provider-specific model name.
            schema_path: Path to a JSON schema for structured output, or ``None``.
            agent: Whether the invocation may use tools / agent capabilities.

        Returns:
            The argv list to execute.
        """

    @abstractmethod
    def parse_response(self, raw: str, response_model: type[BaseModel] | None) -> str | BaseModel:
        """Parse raw CLI stdout into text or a validated model.

        Args:
            raw: Raw stdout from the backend CLI.
            response_model: Model to validate against, or ``None`` for raw text.

        Returns:
            ``raw`` when ``response_model`` is ``None``, else a validated instance.
        """

    @abstractmethod
    def env(self) -> dict[str, str]:
        """Return extra environment variables to set for the CLI invocation."""


class CodexBackend(LlmBackend):
    """:class:`LlmBackend` for the OpenAI ``codex`` CLI."""

    models: ClassVar[dict[TModel, str]] = {
        "small": "gpt-5.3-codex-spark",
        "medium": "gpt-5.4-mini",
        "large": "gpt-5.5",
    }

    def build_command(self, model: str, schema_path: str | None, agent: bool) -> list[str]:
        return [
            "codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--model",
            model,
            *([] if agent else ["-c", "features.codex_hooks=false", "-c", "features.mcp_servers=false"]),
            *(["--output-schema", schema_path] if schema_path else []),
        ]

    def parse_response(self, raw: str, response_model: type[BaseModel] | None) -> str | BaseModel:
        return raw if not response_model else response_model.model_validate_json(raw)

    def env(self) -> dict[str, str]:
        return {}


class ClaudeBackend(LlmBackend):
    """:class:`LlmBackend` for the Anthropic ``claude`` CLI."""

    models: ClassVar[dict[TModel, str]] = {
        "small": "haiku",
        "medium": "sonnet",
        "large": "opus",
    }

    def build_command(self, model: str, schema_path: str | None, agent: bool) -> list[str]:
        return [
            "claude",
            "-p",
            "--no-session-persistence",
            "--model",
            model,
            *(
                ["--permission-mode", "auto", "--max-budget-usd", "1"]
                if agent
                else [
                    "--system-prompt", "",
                    "--setting-sources", "",
                    "--strict-mcp-config",
                ]
            ),
            *(["--json-schema", schema_path, "--output-format", "json"] if schema_path else []),
        ]

    def parse_response(self, raw: str, response_model: type[BaseModel] | None) -> str | BaseModel:
        if not response_model:
            return raw
        data: Any = json.loads(raw)
        if isinstance(data, list) and data:
            return self.extract_structured(
                cast(list[dict[str, Any]], data), response_model
            ) or response_model.model_validate_json(raw)
        return response_model.model_validate_json(raw)

    @staticmethod
    def extract_structured(events: list[dict[str, Any]], model: type[BaseModel]) -> BaseModel | None:
        """Return the validated ``structured_output`` from a stream-json event list, if present."""
        for e in events:
            if e.get("type") == "result" and "structured_output" in e:
                return model.model_validate(e["structured_output"])
        return None

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CODE_SIMPLE": "1"}


class LlmBackends:
    """Registry mapping each :data:`TSpecialty` to the :class:`LlmBackend` that serves it."""

    LLM_BACKENDS: ClassVar[dict[TSpecialty, LlmBackend]] = {
        "debugging": CodexBackend(),
        "review": CodexBackend(),
        "general": ClaudeBackend(),
    }

    @classmethod
    def for_specialty(cls, specialty: TSpecialty) -> LlmBackend:
        """Return the backend registered for ``specialty``."""
        return cls.LLM_BACKENDS[specialty]
