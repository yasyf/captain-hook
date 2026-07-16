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

from cc_transcript.render import clip

from captain_hook.ast_grep import find_all, find_kinds, introduced, lang_for_path
from captain_hook.conditions import workflow_opt_matches, workflow_script_source

if TYPE_CHECKING:
    from collections.abc import Sequence, Set

    from captain_hook.events import BaseHookEvent
    from captain_hook.prompt import Prompt

SNAKE_CASE = re.compile(r"(?<!^)(?=[A-Z])")
WORKFLOW_SCRIPT_CAP = 14_000  # below the prose hooks' max_context=16_000, so truncation stays ours
PIN_EXCERPT_CAP = 2_000  # the pin header must not crowd out the source under the enclosing max_context slice


@dataclass(frozen=True, slots=True)
class Excerpts:
    """Verbatim excerpts pulled from a text under a character budget.

    ``excerpts`` are the kept windows in source order; ``quoted`` counts the spans
    they cover and ``dropped`` the spans excluded once the budget filled. Render with
    :meth:`block`.

    Attributes:
        excerpts: The kept excerpt strings, in source order, without indentation.
        quoted: How many input spans the kept excerpts cover.
        dropped: How many input spans the budget excluded.
    """

    excerpts: tuple[str, ...]
    quoted: int
    dropped: int

    @property
    def capped(self) -> bool:
        """Whether the budget excluded at least one span."""
        return self.dropped > 0

    def block(self, noun: str, *, indent: str = "  ", empty: str = "(none)") -> str:
        """Render the excerpts as an ``indent``-prefixed block.

        A capped block ends in a ``… [+N more <noun> not excerpted]`` marker line; an
        empty one renders the ``empty`` placeholder.

        Args:
            noun: Plural noun for the dropped-count marker, e.g. ``"model pins"``.
            indent: Prefix applied to every line, the marker and placeholder included.
            empty: Placeholder rendered when there are no excerpts.
        """
        lines = [f"{indent}{e}" for e in self.excerpts]
        if self.capped:
            lines.append(f"{indent}… [+{self.dropped} more {noun} not excerpted]")
        return "\n".join(lines) if lines else f"{indent}{empty}"


def excerpt_around(
    text: str,
    spans: Sequence[tuple[int, int]],
    *,
    before: int = 160,
    after: int = 60,
    whole_line_at: int = 200,
    budget: int = 2000,
) -> Excerpts:
    """Verbatim excerpts of ``text`` around each ``(start, end)`` character span.

    Spans are whole-``text`` character offsets — ``re.Match.span()`` is the natural
    producer, and ast-grep callers can pass ``node.range().start.index`` /
    ``node.range().end.index``. Spans group by line: a line at or under
    ``whole_line_at`` characters is quoted whole in one window; a longer line yields
    one window per span, clamped to the line as ``[start - before, end + after]`` and
    merged when windows overlap or touch, each bracketed by ``…`` only where text was
    actually elided. Excerpts accumulate in source order under a greedy character
    ``budget`` — the first excerpt is always kept, and once the budget fills every
    later span is dropped.

    Args:
        text: The source text to excerpt from.
        spans: ``(start, end)`` character spans into ``text``, in source order.
        before: Characters of context kept before each span on a long line.
        after: Characters of context kept after each span on a long line.
        whole_line_at: Lines at or under this length are quoted whole.
        budget: Character budget across the kept excerpts.
    """
    windows: list[tuple[str, int]] = []
    offset = 0
    for line in text.split("\n"):
        local = [(s - offset, e - offset) for s, e in spans if offset <= s < offset + len(line) + 1]
        offset += len(line) + 1
        if not local:
            continue
        if len(line) <= whole_line_at:
            windows.append((line, len(local)))
            continue
        merged: list[list[int]] = []
        for s, e in local:
            lo, hi = max(0, s - before), min(len(line), e + after)
            if merged and lo <= merged[-1][1]:
                merged[-1][1], merged[-1][2] = hi, merged[-1][2] + 1
            else:
                merged.append([lo, hi, 1])
        windows += [(f"{'…' if lo else ''}{line[lo:hi]}{'…' if hi < len(line) else ''}", n) for lo, hi, n in merged]
    used = quoted = dropped = 0
    capped = False
    kept: list[str] = []
    for excerpt, covered in windows:
        if capped or (kept and used + 1 + len(excerpt) > budget):
            capped, dropped = True, dropped + covered
            continue
        used += (1 if kept else 0) + len(excerpt)
        kept.append(excerpt)
        quoted += covered
    return Excerpts(tuple(kept), quoted, dropped)


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


@dataclass(frozen=True, slots=True)
class WorkflowScriptSource:
    """Gating context: the pending ``Workflow`` call's script source, headed by its model pins.

    Resolves the script via :func:`~captain_hook.conditions.workflow_script_source`,
    excerpts the text around every ``model:`` pin into a header capped at
    :data:`PIN_EXCERPT_CAP`, and truncates the body past :data:`WORKFLOW_SCRIPT_CAP`
    with an explicit marker. Yields ``None`` off a Workflow event or an unreadable
    script.
    """

    tag: str = "workflow_script"
    required: bool = True

    @staticmethod
    def pins_and_source(evt: BaseHookEvent) -> tuple[str, str] | None:
        if (source := workflow_script_source(evt)) is None:
            return None
        pins = excerpt_around(source, [m.span() for m in workflow_opt_matches(source, "model")], budget=PIN_EXCERPT_CAP)
        if len(source) > WORKFLOW_SCRIPT_CAP:
            head, tail = WORKFLOW_SCRIPT_CAP * 3 // 4, WORKFLOW_SCRIPT_CAP // 4
            claim = (
                f"{pins.quoted} of {pins.quoted + pins.dropped} model pins quoted above"
                if pins.capped
                else "every model pin is quoted above"
            )
            source = f"{source[:head]}\n… [script truncated: {len(source):,} chars total; {claim}] …\n{source[-tail:]}"
        lead = (
            f"excerpts around the first model pins in this script (+{pins.dropped} more noted below; "
            "a stage not quoted here is NOT necessarily unpinned):"
            if pins.capped
            else "excerpts around every model pin in this script (a stage not quoted here inherits the session model):"
        )
        return f"{lead}\n{pins.block('model pins')}", source

    def content(self, evt: BaseHookEvent) -> str | None:
        if (parts := self.pins_and_source(evt)) is None:
            return None
        header, source = parts
        return f"{header}\n\n{source}"


@dataclass(frozen=True, slots=True)
class UserMessages:
    """The session's user prompts as a ``<user_messages>`` request/authorization record.

    Collects every real user prompt from the transcript — turn-opening text under the
    native classifier, so meta and hook-injected user events are excluded — then renders
    the first prompt followed by the most recent :attr:`last`, deduped where they
    overlap. Each prompt is clipped to :attr:`per_message` characters with an explicit
    ``…(+Nch)`` marker and prefixed ``[first]`` or ``[recent -N]`` (``-1`` most recent)
    so a judge can order them. The first prompt leads so the original ask survives the
    tail clip :func:`apply_contexts` applies. Yields ``None`` when the session carries no
    user prompt; being ``required`` by default, that skips the LLM call rather than
    judging a hook against an empty authorization record.

    Attributes:
        last: How many of the most recent prompts to render after the first.
        per_message: Character budget each rendered prompt is clipped to.
        tag: The XML block tag; defaults to ``user_messages``.
        required: Whether empty content skips the LLM call; defaults to ``True``.

    Example:
        >>> llm_gate("does this edit stay within what the user asked?", contexts=[UserMessages()])
    """

    last: int = 4
    per_message: int = 600
    tag: str = "user_messages"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if not (prompts := [turn.prompt for turn in evt.ctx.transcript.turns if turn.prompt]):
            return None
        recent = range(max(len(prompts) - self.last, 0), len(prompts))
        return "\n\n".join(
            [f"[first]\n{clip(prompts[0], self.per_message)}"]
            + [f"[recent -{len(prompts) - i}]\n{clip(prompts[i], self.per_message)}" for i in recent if i]
        )


def with_defaults(contexts: Sequence[PromptContext]) -> tuple[PromptContext, ...]:
    defaults: tuple[PromptContext, ...] = (BeforeEdit(), AfterEdit())
    return (*contexts, *(d for d in defaults if not any(isinstance(c, type(d)) for c in contexts)))


def apply_contexts(
    prompt: Prompt, evt: BaseHookEvent, contexts: Sequence[PromptContext], *, max_len: int = 2000
) -> Prompt | None:
    """Append each context's block to ``prompt`` in order, each clipped to ``max_len`` characters.

    Over-long content is clipped with an explicit ``…(+Nch)`` marker, never a silent
    truncation. A context yielding ``None`` or whitespace-only content has its block
    omitted; when that context is ``required``, returns ``None`` instead — the caller
    must skip the LLM call entirely.
    """
    for c in contexts:
        text = c.content(evt)
        if c.required and not (text and text.strip()):
            return None
        prompt = prompt.context(c.tag, text and clip(text, max_len))
    return prompt
