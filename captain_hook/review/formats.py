"""The concrete code-review formats the session reviewer recognizes.

The generic :class:`ReviewComment`/:class:`ReviewFormat` types and the
format-dispatch live in :mod:`cc_transcript.mining`; this module supplies
the reviewer's policy — the three review formats it recognizes — and assembles them
into the :class:`~cc_transcript.mining.ReviewSpec` the review-comment detector runs.
The shapes are shared transcript conventions (inline ``In file:Lx:`` cites,
conductor finding blocks, conductor workstream headers) first proven in
cc-pushback's formats module, not cc-pushback specifics.

``conductor-finding`` is a Rust-portable :class:`~cc_transcript.mining.RegexReviewFormat`
(a declarative claim+suggestion group-join); ``superset-inline`` (lookahead) and
``conductor-workstream`` (multi-pass header + body scan) stay
:class:`~cc_transcript.mining.CallableReviewFormat` escape hatches, which keep the
review-comment detector in Python for those formats.
"""

from __future__ import annotations

import re

from cc_transcript.mining.formats import ReviewComment
from cc_transcript.mining.spec import CallableReviewFormat, RegexReviewFormat, ReviewSpec

SUPERSET_INLINE_RE = re.compile(r"^In ((?=\S*[./]|\S+?:L)\S+?)(?::L(\d+)(?:-(\d+))?)?: (.+)$", re.MULTILINE)
CONDUCTOR_WORKSTREAM_HEADER_RE = re.compile(
    r"^### (?P<id>[A-Z][\w-]*\d*)\s*\[(?P<kind>[A-Z]+)\]\s*—\s*(?P<title>.+)$",
    re.MULTILINE,
)
CONDUCTOR_FINDING_FMT = RegexReviewFormat(
    name="conductor-finding",
    groups=(
        (
            "conductor-finding",
            r"^- file: (\S+?):(\d+)\s*$"
            r"(?:\n- theme: .+$)?"
            r"(?:\n- claim: (.+)$)?"
            r"(?:\n- suggestion: (.+)$)?",
        ),
    ),
    file_group=1,
    line_start_group=2,
    line_end_group=None,
    comment_groups=(3, 4),
    join=" ",
    multiline=True,
    ignore_case=False,
)


def extract_superset_inline(text: str) -> tuple[ReviewComment, ...]:
    return tuple(
        ReviewComment(
            file=match.group(1),
            line_start=int(match.group(2)) if match.group(2) else None,
            line_end=int(match.group(3)) if match.group(3) else None,
            comment=match.group(4).strip(),
        )
        for match in SUPERSET_INLINE_RE.finditer(text)
    )


def extract_conductor_workstream(text: str) -> tuple[ReviewComment, ...]:
    headers = list(CONDUCTOR_WORKSTREAM_HEADER_RE.finditer(text))
    return tuple(
        ReviewComment(
            file=None,
            line_start=None,
            line_end=None,
            comment=" ".join(
                [f"{header.group('id')} [{header.group('kind')}] {header.group('title').strip()}"]
                + [
                    line.group(0).strip()
                    for line in re.finditer(r"^(?:FIX|Tests): .+$", text[header.end() : end], re.MULTILINE)
                ]
            ),
        )
        for header, end in zip(
            headers,
            [*(h.start() for h in headers[1:]), len(text)],
            strict=True,
        )
    )


def review_spec() -> ReviewSpec:
    return ReviewSpec(
        regex_formats=(CONDUCTOR_FINDING_FMT,),
        callable_formats=(
            CallableReviewFormat("superset-inline", SUPERSET_INLINE_RE, extract_superset_inline),
            CallableReviewFormat("conductor-workstream", CONDUCTOR_WORKSTREAM_HEADER_RE, extract_conductor_workstream),
        ),
        structured_formats=(),
        surfaces=frozenset({"typed"}),
    )
