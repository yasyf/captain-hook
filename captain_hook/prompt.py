from __future__ import annotations

import inspect
import textwrap
from dataclasses import dataclass
from pathlib import Path

from captain_hook.state import FRAMEWORK_DIR


def dedent_text(text: str) -> str:
    return textwrap.dedent(text).strip()


def caller_dir() -> Path:
    frame = inspect.currentframe()
    while frame and Path(frame.f_code.co_filename).resolve().is_relative_to(FRAMEWORK_DIR):
        frame = frame.f_back
    return Path(frame.f_code.co_filename).resolve().parent if frame else Path.cwd()


@dataclass(frozen=True, kw_only=True)
class Prompt:
    """Fluent builder for structured LLM prompts with system text, XML context sections, and a question.

    Chain ``.system()``, ``.context(tag, content)``, and ``.ask()`` to build prompts.
    ``str()`` renders the full prompt with XML-wrapped context blocks.
    """

    system_text: str = ""
    contexts: tuple[tuple[str, str], ...] = ()
    ask_text: str = ""

    def system(self, text: str) -> Prompt:
        return Prompt(
            system_text=dedent_text(text),
            contexts=self.contexts,
            ask_text=self.ask_text,
        )

    def context(self, tag: str, content: str | None) -> Prompt:
        if content is None or not (normalized := dedent_text(content)):
            return self
        return Prompt(
            system_text=self.system_text,
            contexts=(*self.contexts, (tag, normalized)),
            ask_text=self.ask_text,
        )

    def ask(self, text: str) -> Prompt:
        return Prompt(
            system_text=self.system_text,
            contexts=self.contexts,
            ask_text=dedent_text(text),
        )

    @classmethod
    def from_template(cls, text: str, **vars: object) -> Prompt:
        try:
            return cls(system_text=textwrap.dedent(text).strip().format_map(vars))
        except KeyError as exc:
            raise KeyError(f"template variable {exc.args[0]!r} not supplied") from exc

    @classmethod
    def load(cls, name: str, *, base: str | Path | None = None, **vars: object) -> Prompt:
        """Load a prompt from a ``.md`` file and render it via :meth:`from_template`.

        Resolution searches directories in order, returning the first existing file:
        the ``base`` directory if given (otherwise a ``prompts/`` directory beside the
        calling module), then the framework's bundled ``captain_hook/prompts/``. The
        file path is ``<dir>/<name>.md``; ``name`` may contain ``/`` to nest.

        Args:
            name: Prompt name without the ``.md`` suffix; may include ``/`` for nesting.
            base: Optional directory to search instead of the caller-relative ``prompts/``.
            **vars: Template variables substituted into the file via ``str.format_map``.

        Returns:
            A :class:`Prompt` whose system text is the rendered file contents.

        Raises:
            FileNotFoundError: If no matching file exists in any searched directory.
            KeyError: If the file references a placeholder not supplied in ``**vars``.
        """
        dirs = [Path(base) if base else caller_dir() / "prompts", Path(FRAMEWORK_DIR) / "prompts"]
        for path in (d / f"{name}.md" for d in dirs):
            if path.is_file():
                return cls.from_template(path.read_text(), **vars)
        raise FileNotFoundError(f"prompt {name!r} not found; searched: {', '.join(str(d) for d in dirs)}")

    def __str__(self) -> str:
        parts: list[str] = []
        if self.system_text:
            parts.append(self.system_text)
        for tag, content in self.contexts:
            parts.append(f"<{tag}>\n{content}\n</{tag}>")
        if self.ask_text:
            parts.append(self.ask_text)
        return "\n\n".join(parts)
