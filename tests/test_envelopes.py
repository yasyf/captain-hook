from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from captain_hook.envelopes import (
    ADVISORY_SEPARATOR,
    Envelope,
    block_permission_envelope,
    envelope_text,
    join_block_messages,
)


def test_envelopes_without_framework_imports(tmp_path: Path) -> None:
    script = """
import sys
import captain_hook.envelopes as envelopes

message = envelopes.join_block_messages(("blocked",), ())
envelopes.envelope_text(envelopes.block_permission_envelope("PreToolUse", message))
forbidden = (
    "cc_transcript",
    "captain_hook.types",
    "captain_hook.results",
    "captain_hook.dispatch",
    "captain_hook.app",
    "captain_hook.conditions",
    "captain_hook.state",
    "captain_hook.context",
    "captain_hook.session",
    "captain_hook.snapshots",
    "pydantic",
    "pydantic_core",
    "loguru",
)
assert not {
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
}
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("message", ["blocked", None])
def test_block_permission_envelope(message: str | None) -> None:
    assert block_permission_envelope("PreToolUse", message) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


@pytest.mark.parametrize(
    ("blocks", "advisories", "expected"),
    [
        ((), (), None),
        (("",), (), None),
        (("first", "second", "first"), (), "first\n\nsecond\n\nfirst"),
        (
            (),
            ("advice",),
            "Additional advisories (not the reason for the deny):\n\nadvice",
        ),
        (
            ("blocked",),
            ("second advice", "first advice", "second advice"),
            "blocked\n\nAdditional advisories (not the reason for the deny):\n\nsecond advice\n\nfirst advice\n\nsecond advice",
        ),
    ],
    ids=["empty", "empty-message", "ordered-duplicates", "advisory-only", "ordered-advisories"],
)
def test_join_block_messages(blocks: tuple[str, ...], advisories: tuple[str, ...], expected: str | None) -> None:
    assert join_block_messages(blocks, advisories) == expected


def test_envelope_text() -> None:
    assert envelope_text("keep whitespace\n") == "keep whitespace\n"
    assert envelope_text({"message": "café"}) == '{"message": "caf\\u00e9"}'


def test_framework_reexports_share_objects() -> None:
    import captain_hook.dispatch as dispatch
    import captain_hook.results as results

    assert dispatch.Envelope is results.Envelope is Envelope
    assert dispatch.envelope_text is results.envelope_text is envelope_text
    assert dispatch.ADVISORY_SEPARATOR is results.ADVISORY_SEPARATOR is ADVISORY_SEPARATOR
