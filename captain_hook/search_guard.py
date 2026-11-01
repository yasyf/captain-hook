from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cc_transcript.command import parse_command_line
from cc_transcript.tools import BashCall, parse_tool_call

from captain_hook.cmd import Cmd
from captain_hook.envelopes import Envelope, block_permission_envelope, envelope_text, join_block_messages
from captain_hook.search_policy import blocked_messages
from captain_hook.util.shell import safe_parse_command_line

if TYPE_CHECKING:
    from collections.abc import Callable

PERFORMANCE_ROOT = Path(__file__).parent / "builtin_packs" / "performance" / "hooks"


def evaluate(raw: dict[str, Any], pack_roots: Callable[[], tuple[Path, ...]]) -> Envelope | None:
    cwd = Path(value) if (value := raw.get("cwd")) else None
    tool_name = raw.get("tool_name") or ""
    call = parse_tool_call(tool_name, raw.get("tool_input", {}), on_error="other")
    command = call.command if isinstance(call, BashCall) else ""
    line = safe_parse_command_line(command)
    cmd = Cmd(line if line is not None else parse_command_line(""), raw=command, cwd=cwd)
    messages = blocked_messages(cmd, tool_name, call.raw, cwd, pack_roots=pack_roots)
    return block_permission_envelope("PreToolUse", join_block_messages(messages, ())) if messages else None


def check(raw: dict[str, Any]) -> Envelope | None:
    from captain_hook.app import registered_pack_roots

    return evaluate(raw, lambda: (*registered_pack_roots(), PERFORMANCE_ROOT))


def main() -> None:
    if output := evaluate(json.load(sys.stdin), lambda: (PERFORMANCE_ROOT,)):
        print(envelope_text(output))
