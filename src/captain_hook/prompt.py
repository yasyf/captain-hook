from __future__ import annotations

import textwrap
from dataclasses import dataclass


def dedent_text(text: str) -> str:
    return textwrap.dedent(text).strip()


@dataclass(frozen=True, kw_only=True)
class PromptMessage:
    """Fluent builder for structured LLM prompts with system text, XML context sections, and a question.

    Chain ``.system()``, ``.context(tag, content)``, and ``.ask()`` to build prompts.
    ``str()`` renders the full prompt with XML-wrapped context blocks.
    """

    system_text: str = ""
    contexts: tuple[tuple[str, str], ...] = ()
    ask_text: str = ""

    def system(self, text: str) -> PromptMessage:
        return PromptMessage(
            system_text=dedent_text(text),
            contexts=self.contexts,
            ask_text=self.ask_text,
        )

    def context(self, tag: str, content: str | None) -> PromptMessage:
        if content is None or not (normalized := dedent_text(content)):
            return self
        return PromptMessage(
            system_text=self.system_text,
            contexts=(*self.contexts, (tag, normalized)),
            ask_text=self.ask_text,
        )

    def ask(self, text: str) -> PromptMessage:
        return PromptMessage(
            system_text=self.system_text,
            contexts=self.contexts,
            ask_text=dedent_text(text),
        )

    @classmethod
    def from_template(cls, text: str, **vars: object) -> PromptMessage:
        try:
            return cls(system_text=textwrap.dedent(text).strip().format_map(vars))
        except KeyError as exc:
            raise KeyError(f"template variable {exc.args[0]!r} not supplied") from exc

    def __str__(self) -> str:
        parts: list[str] = []
        if self.system_text:
            parts.append(self.system_text)
        for tag, content in self.contexts:
            parts.append(f"<{tag}>\n{content}\n</{tag}>")
        if self.ask_text:
            parts.append(self.ask_text)
        return "\n\n".join(parts)


Prompt = PromptMessage
