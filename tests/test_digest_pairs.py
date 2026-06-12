from __future__ import annotations

import json
from pathlib import Path

from cc_transcript.ids import tool_digest

PAIRS = Path(__file__).parent / "testdata" / "hook_transcript_pairs.jsonl"


def test_hook_stdin_and_transcript_digests_agree() -> None:
    """The cross-source digest identity: a tool call's hook-stdin payload and its
    transcript ToolUseBlock must produce the same content digest.

    The corpus is recorded from a live session (scratch dir + a plain ``tee``
    PreToolUse hook, paired against the session transcript by digest) — re-record
    across Claude Code version bumps so normalization drift is caught here before
    it is caught in production miners.
    """
    pairs = [json.loads(line) for line in PAIRS.read_text().splitlines() if line.strip()]
    assert pairs, "pair corpus must not be empty"
    for pair in pairs:
        stdin_digest = tool_digest(pair["stdin_tool_name"], pair["stdin_tool_input"])
        transcript_digest = tool_digest(pair["transcript_tool_name"], pair["transcript_tool_input"])
        assert stdin_digest == transcript_digest == pair["digest"]
        assert pair["stdin_tool_input"] == pair["transcript_tool_input"]
