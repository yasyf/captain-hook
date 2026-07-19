#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///
"""Regenerates docs/assets/demo.termshow — the animated sibling of demo.png.

Recreates the same scenario as demo.sh, deterministically: stages a scratch
scaffold (never this repo's own hooks) with one force-push guard, replays the
exact PreToolUse payload Claude Code sends for `git push --force`, then runs the
hook's inline tests. The real captured output is woven into a `.termshow`
recording (newline-delimited JSON: a header line, then `[interval, code, data]`
event arrays) with a synthetic typewriter animation, so the frame content is
authentic while the timing stays byte-stable across runs.

Discovery is scoped hermetically with an empty $CLAUDE_CONFIG_DIR so only the
scratch hook's two tests run — never this machine's globally enabled packs.

Requires: uv (for `uvx capt-hook`). Run it with `uv run docs/scripts/demo-termshow.py`.
Render/preview with `great-docs termshow render docs/assets/demo.termshow`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUTPUT = REPO / "docs" / "assets" / "demo.termshow"

COLS, ROWS = 100, 12

# The same guard demo.sh writes, kept byte-identical so both demos show one scenario.
HOOK = """\
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "push", "--force"],
    reason="Force-pushing rewrites shared history",
    hint="Use `git push --force-with-lease` instead",
    tests={
        Input(command="git push --force"): Block(),
        Input(command="git push origin main"): Allow(),
    },
)
"""

PAYLOAD = '{"tool_name": "Bash", "tool_input": {"command": "git push --force"}}'

# The command lines exactly as the viewer should read them (cosmetic display).
CMD1_LINE1 = f"$ echo '{PAYLOAD}' |"
CMD1_LINE2 = "    uvx capt-hook run PreToolUse | jq -r .hookSpecificOutput.permissionDecisionReason"
CMD2 = "$ uvx capt-hook test"

BOLD, RESET = "\x1b[1m", "\x1b[0m"


def capture(scratch: Path, config: Path) -> tuple[str, list[str]]:
    """Run the two real commands hermetically and return (blocked reason, test lines)."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config)}
    kw = {"cwd": scratch, "env": env, "text": True, "capture_output": True}

    # Warm the uvx tool environment so the resolver spinner never leaks into a capture.
    subprocess.run(["uvx", "capt-hook", "test"], **kw)

    replay = subprocess.run(["uvx", "capt-hook", "run", "PreToolUse"], input=PAYLOAD, **kw)
    reason = json.loads(replay.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    tests = subprocess.run(["uvx", "capt-hook", "test"], **kw)
    return reason, tests.stdout.splitlines()


class Reel:
    """Accumulates termshow output events as relative-interval [gap, "o", data] arrays."""

    def __init__(self) -> None:
        self.events: list[list[object]] = []

    def out(self, gap: float, data: str) -> None:
        self.events.append([round(gap, 3), "o", data])

    def type_line(self, text: str, *, lead: float, cps: float, bold: bool = False) -> None:
        """Reveal a line one character at a time, then break to the next row."""
        for i, ch in enumerate(text):
            head = BOLD if (bold and i == 0) else ""
            self.out(lead if i == 0 else cps, head + ch)
        self.out(cps, (RESET if bold else "") + "\r\n")

    def print_line(self, text: str, *, lead: float) -> None:
        """Emit a full output line at once (command results arrive whole)."""
        self.out(lead, text + "\r\n")


def build_reel(reason: str, test_lines: list[str]) -> Reel:
    r = Reel()

    # Prompt one: replay the PreToolUse payload, watch the guard block it.
    r.type_line(CMD1_LINE1, lead=0.5, cps=0.022, bold=True)
    r.type_line(CMD1_LINE2, lead=0.05, cps=0.02, bold=True)
    r.print_line(reason, lead=0.55)

    # Prompt two: prove the guard with its inline tests.
    r.print_line("", lead=0.3)
    r.type_line(CMD2, lead=1.1, cps=0.05, bold=True)
    for i, line in enumerate(test_lines):
        r.print_line(line, lead=0.5 if i == 0 else 0.14)

    # Hold the final frame so the last read lands.
    r.out(2.2, "")
    return r


def write_termshow(reel: Reel) -> None:
    header = {
        "version": 1,
        "format": "termshow",
        "term": {"cols": COLS, "rows": ROWS, "type": "xterm-256color"},
        "title": "captain-hook demo",
    }
    lines = [json.dumps(header)]
    lines += [json.dumps(e) for e in reel.events]
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="capt-hook-demo-"))
    config = Path(tempfile.mkdtemp(prefix="capt-hook-cfg-"))
    try:
        (scratch / ".claude" / "hooks").mkdir(parents=True)
        (scratch / ".claude" / "hooks" / "safety.py").write_text(HOOK, encoding="utf-8")
        reason, test_lines = capture(scratch, config)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(config, ignore_errors=True)

    reel = build_reel(reason, test_lines)
    write_termshow(reel)

    duration = sum(e[0] for e in reel.events)
    print(f"wrote {OUTPUT.relative_to(REPO)} — {len(reel.events)} events, {duration:.1f}s")


if __name__ == "__main__":
    main()
