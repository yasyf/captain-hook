from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel

TSpecialty = Literal["debugging", "review", "general"]
TModel = Literal["small", "medium", "large"]


class LlmBackend(ABC):
    models: ClassVar[dict[TModel, str]]

    @abstractmethod
    def build_command(self, model: str, schema_path: str | None, agent: bool) -> list[str]: ...

    @abstractmethod
    def parse_response(self, raw: str, response_model: type[BaseModel] | None) -> str | BaseModel: ...

    @abstractmethod
    def env(self) -> dict[str, str]: ...


class CodexBackend(LlmBackend):
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
        for e in events:
            if e.get("type") == "result" and "structured_output" in e:
                return model.model_validate(e["structured_output"])
        return None

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CODE_SIMPLE": "1"}


class LlmBackends:
    LLM_BACKENDS: ClassVar[dict[TSpecialty, LlmBackend]] = {
        "debugging": CodexBackend(),
        "review": CodexBackend(),
        "general": ClaudeBackend(),
    }

    @classmethod
    def for_specialty(cls, specialty: TSpecialty) -> LlmBackend:
        return cls.LLM_BACKENDS[specialty]
