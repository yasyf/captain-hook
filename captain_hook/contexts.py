"""Declarative prompt contexts: named XML evidence blocks attached to LLM primitives.

A :class:`PromptContext` resolves one ``<tag>…</tag>`` block from the current event at
evaluation time. Pass instances via ``contexts=[...]`` on ``llm_nudge``/``llm_gate``;
a ``required`` context whose content is empty skips the LLM call entirely. The ambient
defaults :class:`BeforeEdit` and :class:`AfterEdit` attach to every primitive, carrying
the pending edit's before/after text on edit-shaped events and nothing elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Protocol

from captain_hook.ast_grep import find_all, find_kinds, introduced, lang_for_path

if TYPE_CHECKING:
    from collections.abc import Sequence, Set

    from captain_hook.events import BaseHookEvent
    from captain_hook.prompt import Prompt

SNAKE_CASE = re.compile(r"(?<!^)(?=[A-Z])")


class PromptContext(Protocol):
    """One declarative XML block attached to an LLM primitive's prompt.

    ``content(evt)`` runs at evaluation time; ``None`` or empty content omits the
    block. When a ``required`` context yields empty content, the primitive skips
    the LLM call entirely — nothing fires and no fire is consumed.
    """

    tag: str
    required: bool

    def content(self, evt: BaseHookEvent) -> str | None: ...


@dataclass(frozen=True, slots=True)
class BeforeEdit:
    """The pending edit's pre-image, as a ``<before_edit>`` block.

    Reads :attr:`~captain_hook.events.ToolHookEvent.replaced` — an Edit's old text or
    a MultiEdit's joined olds at any event, a Write's current on-disk content only at
    ``PreToolUse`` (after the Write lands, disk holds the new text). The block is
    omitted whenever the pre-image is unknowable: non-edit events, Writes off
    ``PreToolUse``, unreadable paths. Attached to every LLM primitive as a default
    context; ``required=False`` because it is ambient enrichment. Pass
    ``BeforeEdit(required=True)`` explicitly to gate a hook on a non-empty pre-image.
    """

    tag: str = "before_edit"
    required: bool = False

    def content(self, evt: BaseHookEvent) -> str | None:
        return evt.replaced


@dataclass(frozen=True, slots=True)
class AfterEdit:
    """The pending edit's new text, as an ``<after_edit>`` block.

    Reads :attr:`~captain_hook.events.BaseHookEvent.content` — the text an Edit,
    Write, MultiEdit, or NotebookEdit is about to land. Attached to every LLM
    primitive as a default context; ``required=False`` because it is ambient
    enrichment, empty (and omitted) on non-edit events.
    """

    tag: str = "after_edit"
    required: bool = False

    def content(self, evt: BaseHookEvent) -> str | None:
        return evt.content


@dataclass(frozen=True, slots=True)
class Introduced:
    """Constructs the pending edit newly introduces, as an auto-tagged block.

    Exactly one of ``kind``/``pattern`` selects the constructs: ``kind`` names
    tree-sitter node kinds (a bare string or any set — normalized to ``frozenset``;
    see :data:`~captain_hook.ast_grep.COMMENT_TYPES`), ``pattern`` is an ast-grep
    pattern. Extraction diffs the event's before/after text, so only constructs
    absent before the edit appear; files without a supported language yield nothing.
    The pre-image comes from ``evt.replaced``, so hooks covering Writes need
    ``events=Event.PreToolUse`` — at any other event a Write's pre-image is
    unknowable, the context yields ``None``, and (being ``required``) the LLM call
    skips rather than misreporting every construct as introduced.

    ``required`` defaults to ``True``: a context you attach explicitly IS the
    evidence — no evidence, no LLM call. ``tag`` auto-derives from the class name in
    snake_case (``Introduced()`` renders ``<introduced>``; a subclass named
    ``TombstoneComments`` renders ``<tombstone_comments>``). Subclass and override
    :meth:`keep` to filter which introduced constructs count.

    Example:
        >>> llm_nudge("...", contexts=[Introduced(pattern="print($$$)")],
        ...           events=Event.PreToolUse, only_if=[Tool("Edit", "Write", "MultiEdit")])
    """

    kind: str | Set[str] | None = None
    pattern: str | None = None
    required: bool = True
    tag: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is None) == (self.pattern is None):
            raise ValueError(f"{type(self).__name__} requires exactly one of kind or pattern")
        if self.kind is not None and not isinstance(self.kind, frozenset):
            object.__setattr__(self, "kind", frozenset([self.kind] if isinstance(self.kind, str) else self.kind))
        if self.tag is None:
            object.__setattr__(self, "tag", SNAKE_CASE.sub("_", type(self).__name__).lower())

    def keep(self, text: str) -> bool:
        """Whether an introduced construct's text belongs in the block — override to filter."""
        return True

    def content(self, evt: BaseHookEvent) -> str | None:
        if (
            not (file := evt.file)
            or not (lang := lang_for_path(file.path))
            or (old := evt.replaced) is None
            or (new := evt.content) is None
        ):
            return None
        extract = (
            partial(find_kinds, lang=lang, kinds=self.kind)
            if self.pattern is None
            else partial(find_all, lang=lang, pattern=self.pattern)
        )
        return "\n".join(m.text for m in introduced(extract(old), extract(new)) if self.keep(m.text))


def with_defaults(contexts: Sequence[PromptContext]) -> tuple[PromptContext, ...]:
    defaults: tuple[PromptContext, ...] = (BeforeEdit(), AfterEdit())
    return (*contexts, *(d for d in defaults if not any(isinstance(c, type(d)) for c in contexts)))


def apply_contexts(
    prompt: Prompt, evt: BaseHookEvent, contexts: Sequence[PromptContext], *, max_len: int = 2000
) -> Prompt | None:
    """Append each context's block to ``prompt`` in order, each capped at ``max_len`` characters.

    A context yielding ``None`` or whitespace-only content has its block omitted;
    when that context is ``required``, returns ``None`` instead — the caller must
    skip the LLM call entirely.
    """
    for c in contexts:
        text = c.content(evt)
        if c.required and not (text and text.strip()):
            return None
        prompt = prompt.context(c.tag, text and text[:max_len])
    return prompt
