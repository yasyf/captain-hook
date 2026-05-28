from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings

from captain_hook._backends import CodexBackend, LlmBackend, LlmBackends, TModel, TSpecialty
from captain_hook.prompt import PromptMessage
from captain_hook.session import SessionStore

if TYPE_CHECKING:
    from captain_hook.transcript import Transcript, TranscriptSlice, Turn


@dataclass
class HookContext:
    """Runtime context injected into every hook event, providing session state, transcript, settings, and LLM/CLI helpers."""

    session: SessionStore
    transcript: Transcript
    settings: BaseSettings | None
    project_root: Path | None = None

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
            err.add_note(f"argv: {args}")
            err.add_note(f"exit_code: {result.returncode}")
            err.add_note(f"stderr: {result.stderr[-4096:]}")
            err.add_note(f"stdout: {result.stdout[-4096:]}")
            raise err
        return result.stdout

    def git(self, *args: str) -> str | None:
        try:
            return self.call_cli(["git", *args], timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

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
        schema = (
            json.dumps(response_model.model_json_schema() | {"additionalProperties": False})
            if response_model
            else None
        )
        backend = LlmBackends.for_specialty(specialty)
        schema_path = self.resolve_schema_path(backend, schema)

        cmd = backend.build_command(backend.models[model], schema_path, agent)
        raw = self.call_cli(cmd, input=prompt, timeout=timeout, env=backend.env())
        return backend.parse_response(raw, response_model)

    @staticmethod
    def resolve_schema_path(backend: LlmBackend, schema: str | None) -> str | None:
        if not schema:
            return None
        if isinstance(backend, CodexBackend):
            fd, path = tempfile.mkstemp(suffix=".json")
            os.write(fd, schema.encode())
            os.close(fd)
            return path
        return schema
