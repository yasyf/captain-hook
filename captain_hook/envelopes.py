from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):"

type Envelope = dict[str, Any] | str


def block_permission_envelope(event_name: str, message: str | None) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def join_block_messages(blocks: Sequence[str], deny_advisories: Sequence[str]) -> str | None:
    parts = list(blocks)
    if deny_advisories:
        parts.append(ADVISORY_SEPARATOR)
        parts.extend(deny_advisories)
    return "\n\n".join(parts) or None


def envelope_text(envelope: Envelope) -> str:
    return envelope if isinstance(envelope, str) else json.dumps(envelope)
