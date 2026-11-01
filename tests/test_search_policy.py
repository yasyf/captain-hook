from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
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


@pytest.mark.parametrize(
    ("command", "blocked"),
    (
        ("rg -l 'perf-46725-proof|46772' {corpus}", True),
        ("rg needle ~/.cc-inbox/blobs", True),
        ('rg needle "$HOME/.cc-inbox/blobs"', True),
        ("cd ~/.cc-inbox/blobs && rg needle .", True),
        ("rg needle {home}/.cc-inbox", True),
        ("rg needle {alias}", True),
        ("rg needle {corpus}/known-digest", False),
        ("rg needle ~/.cc-inbox/blobs/known-digest", False),
        ("rg --glob known-digest needle {corpus}", False),
        ("rg -g 'known-*' needle {corpus}", False),
    ),
    ids=("reported", "home", "variable", "cwd", "parent", "symlink", "file", "home-file", "filename", "prefix"),
)
def test_blob_corpus_search_requires_file_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, blocked: bool
) -> None:
    home = tmp_path / "home"
    corpus = home / ".cc-inbox" / "blobs"
    corpus.mkdir(parents=True)
    (corpus / "known-digest").write_text("needle")
    alias = tmp_path / "corpus-alias"
    alias.symlink_to(corpus, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    raw = command.format(home=home, corpus=corpus, alias=alias)
    cmd = Cmd(parse_command_line(raw), raw=raw, cwd=tmp_path)

    assert blocked_messages(cmd, "Bash", {}, tmp_path, pack_roots=lambda: ()) == ((SEARCH_MESSAGE,) if blocked else ())
