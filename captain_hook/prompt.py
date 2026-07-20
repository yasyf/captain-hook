from __future__ import annotations

import inspect
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from captain_hook.state import FRAMEWORK_DIR, framework_frame
from captain_hook.util import reqenv

PLACEHOLDER = re.compile(r"(?<![{$])\{([A-Za-z_]\w*)\}(?!\})")


def dedent_text(text: str) -> str:
    return textwrap.dedent(text).strip()


def escape_tag_delimiters(tag: str, content: str) -> str:
    return re.sub(
        rf"<\s*(/?)\s*{re.escape(tag)}\s*>",
        lambda m: f"&lt;{m.group(1)}{tag}&gt;",
        content,
        flags=re.IGNORECASE,
    )


def caller_dir() -> Path:
    frame = inspect.currentframe()
    while frame and framework_frame(frame.f_code.co_filename):
        frame = frame.f_back
    return Path(frame.f_code.co_filename).resolve().parent if frame else reqenv.cwd()


def render_template(text: str, **vars: object) -> str:
    def repl(match: re.Match[str]) -> str:
        if (name := match.group(1)) not in vars:
            raise KeyError(f"template variable {name!r} not supplied")
        return str(vars[name])

    return PLACEHOLDER.sub(repl, text)


@dataclass(frozen=True, kw_only=True)
class Prompt:
    """Fluent builder for structured LLM prompts with system text, XML context sections, and a question.

    Chain ``.system()``, ``.context(tag, content)``, and ``.ask()`` to build prompts.
    ``str()`` renders the full prompt with XML-wrapped context blocks. Context content is
    treated as untrusted data: any occurrence of a block's own tag delimiters inside its
    content is entity-escaped at render time, so embedded text can never close the block
    early and inject instructions outside it.
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
        """Render ``text`` into a system-only prompt, substituting ``{identifier}`` placeholders from ``vars``.

        Only ``{identifier}`` (a brace-wrapped Python identifier) is a placeholder; every other
        brace is literal. JavaScript object braces (``{model: 'sonnet'}``), ``${shell}``
        interpolations, empty ``{}``, and doubled ``{{x}}`` braces all pass through unchanged —
        there is no escape sequence and format specs are not placeholders. Substitution is
        single-pass, so inserted values are never re-scanned for further placeholders.

        Args:
            text: Template text; ``{identifier}`` placeholders are replaced, all other braces stay literal.
            **vars: Values for the ``{identifier}`` placeholders.

        Returns:
            A :class:`Prompt` whose system text is the rendered template.

        Raises:
            KeyError: If an ``{identifier}`` placeholder has no matching entry in ``vars``.
        """

        return cls(system_text=render_template(dedent_text(text), **vars))

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
            **vars: Values for the file's ``{identifier}`` placeholders; every other brace (JS
                objects, ``${...}``, ``{{...}}``) stays literal (see :meth:`from_template`).

        Returns:
            A :class:`Prompt` whose system text is the rendered file contents.

        Raises:
            FileNotFoundError: If no matching file exists in any searched directory.
            KeyError: If the file references an ``{identifier}`` placeholder not supplied in ``**vars``.
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
            parts.append(f"<{tag}>\n{escape_tag_delimiters(tag, content)}\n</{tag}>")
        if self.ask_text:
            parts.append(self.ask_text)
        return "\n\n".join(parts)
