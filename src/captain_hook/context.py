from __future__ import annotations

import json
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from pydantic import BaseModel
from pydantic_settings import BaseSettings

from captain_hook.prompt import PromptMessage
from captain_hook.session import SessionStore

if TYPE_CHECKING:
    from captain_hook.transcript import Transcript, TranscriptSlice, Turn

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
            *(["--permission-mode", "auto", "--max-budget-usd", "1"] if agent else ["--bare"]),
            *(["--json-schema", schema_path, "--output-format", "json"] if schema_path else []),
        ]

    def parse_response(self, raw: str, response_model: type[BaseModel] | None) -> str | BaseModel:
        if not response_model:
            return raw
        data: Any = json.loads(raw)
        if isinstance(data, list) and data:
            return self._extract_structured(
                cast(list[dict[str, Any]], data), response_model
            ) or response_model.model_validate_json(raw)
        return response_model.model_validate_json(raw)

    @staticmethod
    def _extract_structured(events: list[dict[str, Any]], model: type[BaseModel]) -> BaseModel | None:
        for e in events:
            if e.get("type") == "result" and "structured_output" in e:
                return model.model_validate(e["structured_output"])
        return None

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CODE_SIMPLE": "1"}


BACKENDS: dict[TSpecialty, LlmBackend] = {
    "debugging": CodexBackend(),
    "review": CodexBackend(),
    "general": ClaudeBackend(),
}


@dataclass
class HookContext:
    """Runtime context injected into every hook event, providing session state, transcript, settings, and LLM/CLI helpers."""

    session: SessionStore
    transcript: Transcript
    settings: BaseSettings | None

    @property
    def t(self) -> Transcript:
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
    def conf(self) -> BaseSettings | None:
        """Alias for ``settings``."""
        return self.settings

    @property
    def c(self) -> BaseSettings | None:
        """Alias for ``settings`` (shortest form)."""
        return self.conf

    @cached_property
    def turn(self) -> Turn:
        """The current transcript turn (cached)."""
        return self.transcript.current_turn

    @cached_property
    def prior(self) -> TranscriptSlice:
        """Transcript slice before the current turn (cached)."""
        return self.transcript.prior()

    def call_cli(
        self,
        args: list[str],
        *,
        input: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> str:
        result = subprocess.run(
            args,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ | (env or {}),
            cwd=os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR"),
        )
        if result.returncode != 0:
            err = subprocess.CalledProcessError(
                result.returncode,
                args,
                output=result.stdout,
                stderr=result.stderr,
            )
            err.add_note(f"stderr: {result.stderr[:2000]}")
            err.add_note(f"stdout: {result.stdout[:2000]}")
            raise err
        return result.stdout

    def call_llm(
        self,
        template: str | PromptMessage,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool = False,
        agent: bool = False,
        response_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> str | BaseModel:
        if isinstance(template, PromptMessage):
            prompt = str(template)
            if transcript:
                prompt = f"{self.transcript}\n\n<task>\n{prompt}\n</task>"
        else:
            if transcript:
                template = f"{{transcript}}\n\n<task>\n{template}\n</task>"
            prompt = template.format(*args, **kwargs, transcript=self.transcript)
        schema = response_model and json.dumps(
            response_model.model_json_schema() | {"additionalProperties": False},
        )
        backend = BACKENDS[specialty]
        schema_path = self._resolve_schema_path(backend, schema)

        cmd = backend.build_command(backend.models[model], schema_path, agent)
        raw = self.call_cli(cmd, input=prompt, timeout=timeout, env=backend.env())
        return backend.parse_response(raw, response_model)

    @staticmethod
    def _resolve_schema_path(backend: LlmBackend, schema: str | None) -> str | None:
        if not schema:
            return None
        if isinstance(backend, CodexBackend):
            fd, path = tempfile.mkstemp(suffix=".json")
            os.write(fd, schema.encode())
            os.close(fd)
            return path
        return schema
