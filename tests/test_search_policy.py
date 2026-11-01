from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cc_transcript.command import parse_command_line

from captain_hook.cmd import Cmd
from captain_hook.search_policy import FIND_MESSAGE, SEARCH_MESSAGE, blocked_messages, known_search_roots


def test_search_policy_import_without_framework(tmp_path: Path) -> None:
    script = """
import sys
import captain_hook.search_policy

forbidden = (
    "captain_hook.app",
    "captain_hook.state",
    "captain_hook.types",
    "captain_hook.command_schema",
    "captain_hook.conditions",
    "captain_hook.context",
    "captain_hook.session",
    "captain_hook.snapshots",
    "captain_hook.loader",
    "captain_hook.results",
    "captain_hook.dispatch",
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


def test_pack_roots_provider_is_read_on_each_evaluation(tmp_path: Path) -> None:
    roots = [tmp_path / "first"]

    def pack_roots() -> tuple[Path, ...]:
        return tuple(roots)

    assert known_search_roots(None, pack_roots=pack_roots) == (tmp_path / "first",)
    roots.append(tmp_path / "second")
    assert known_search_roots(None, pack_roots=pack_roots) == (tmp_path / "first", tmp_path / "second")


def test_blocked_messages_keeps_shell_rule_order(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    raw = "rg needle .; find /"
    cmd = Cmd(parse_command_line(raw), raw=raw, cwd=tmp_path)

    def pack_roots() -> tuple[Path, ...]:
        return ()

    assert blocked_messages(cmd, "Bash", {}, tmp_path, pack_roots=pack_roots) == (SEARCH_MESSAGE, FIND_MESSAGE)
