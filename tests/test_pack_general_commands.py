from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

import pytest

from captain_hook.dispatch import dispatch
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.types import Event

if TYPE_CHECKING:
    from pathlib import Path

COMMANDS_MODULE = "captain_hook.builtin_packs.general.hooks.commands"

STASH_CORPUS = [
    ("git stash", True),
    ("git stash -u", True),
    ("git stash -k", True),
    ("git stash --include-untracked", True),
    ("git stash push", True),
    ("git stash push -u", True),
    ("git stash push -- file.py", True),
    ("git stash pop", True),
    ("git stash pop --index", True),
    ("git stash pop stash@{0}", True),
    ("git stash clear", True),
    ("git stash save wip", True),
    ("git stash branch topic", True),
    ("git stash create", True),
    ("git stash store 0c4f3a1", True),
    ("git stash apply", True),
    ("git stash apply --index", True),
    ("git stash drop", True),
    ("git stash drop -q", True),
    ("git -C /repo stash", True),
    ("git -C /repo stash pop", True),
    ("git -c core.pager=cat stash pop", True),
    ("git --git-dir=/r/.git stash pop", True),
    ("git --git-dir /r/.git stash pop", True),
    ("git --no-pager stash pop", True),
    ("git -P stash pop", True),
    ("git -C /r -c a=b stash", True),
    ("git status && git stash", True),
    ("git stash list && git stash pop", True),
    ("cd /repo && git stash -u", True),
    ("sh -c 'git stash pop'", True),
    ("sudo git stash", True),
    ("git stash list", False),
    ("git stash list --stat", False),
    ("git stash list --format='%H %gs'", False),
    ("git stash show", False),
    ("git stash show -p", False),
    ("git stash show -p stash@{0}", False),
    ("git stash show --stat stash@{1}", False),
    ("git -C /repo stash list", False),
    ("git --no-pager stash list", False),
    ("git stash --help", False),
    ("git stash -h", False),
    ("git stash push -h", False),
    ("git stash pop --help", False),
    ("git --help stash pop", False),
    ('git stash push -m "lane"', False),
    ('git stash push -u -m "lane"', False),
    ('git stash push -um "lane"', False),
    ('git stash push -km "lane"', False),
    ('git stash push --message "lane"', False),
    ("git stash push --message=lane", False),
    ("git stash push --keep-index -m lane", False),
    ("git stash push --include-untracked --message lane", False),
    ("git stash push -m lane -- file.py", False),
    ("git stash push -m lane file.py", False),
    ('git stash -m "lane"', False),
    ('git stash -u -m "lane"', False),
    ('git stash -um "lane"', False),
    ("git stash --message=lane", False),
    ("git -C /repo stash push -m lane", False),
    ("git -c a=b stash push -m lane", False),
    ("git --no-pager stash push -u -m lane", False),
    ("git stash apply 0c4f3a1", False),
    ("git stash apply stash@{1}", False),
    ("git stash apply --index stash@{1}", False),
    ("git stash apply -q 0c4f3a1", False),
    ("git stash drop stash@{2}", False),
    ("git stash drop -q 2", False),
    ("git stash drop 0c4f3a1", False),
    ("git -C /repo stash apply 0c4f3a1", False),
    ("git stash drop -q $(git stash list | grep tag | cut -d: -f1)", True),
    ("git stash drop -q $(git stash list | grep missing-tag | cut -d: -f1)", True),
    ("git stash apply -q $(git stash list --format='%H %gs' | grep tag | cut -d' ' -f1)", False),
    ("git stash drop `git stash list | grep tag | cut -d: -f1`", True),
    ("git stash pop $(git stash list | grep tag | cut -d: -f1)", True),
    ("git stash $(echo pop)", True),
    ("git stash drop --bogus", True),
    ("git stash list | grep tag", False),
    ("git stash list --format='%gd %gs' | grep tag | cut -d' ' -f1", False),
    ("git -C /repo stash list | head", False),
    ("git status && git stash list", False),
    ("git stash list && git stash show -p stash@{1}", False),
    ("git -C /repo stash show -p stash@{3} | head -50", False),
    ("ref=$(git stash list --format='%H %gs' | grep tag | cut -d' ' -f1)", False),
    (
        "cd /Users/yasyf/.claude/worktrees/captain-hook/transcript-tail && timeout 600 uv run pytest -q "
        '-p no:cacheprovider "tests/test_transcripts_registration.py::TestMcpTool" 2>&1 | tail -2; '
        "git stash push -q -m tcap-check-$$ && timeout 600 uv run pytest -q -p no:cacheprovider "
        "tests/test_transcripts_registration.py 2>&1 | tail -2; "
        "ref=$(git stash list --format='%H %gs' | grep \"tcap-check-$$\" | cut -d' ' -f1); "
        "git stash apply -q $ref && git stash drop -q $(git stash list --format='%gd %gs' | "
        "grep \"tcap-check-$$\" | cut -d' ' -f1) && git diff --stat | tail -1",
        True,
    ),
    (
        "cd ~/.claude/worktrees/cc-context/ccx-followups && git stash push -q -m ccx-followups-test -- "
        "internal/cli/shipsubmit_test.go && git cherry-pick 23b01e9d84 2>&1 | tail -3; git status --short | head; "
        "S=$(git stash list --format='%H %gs' | grep ccx-followups-test | cut -d' ' -f1); "
        "git stash apply -q $S && git stash drop -q $(git stash list | grep ccx-followups-test | cut -d: -f1); "
        "git status --short",
        True,
    ),
    ("git status", False),
    ("git stash-helper pop", False),
    ("git log --stash", False),
    ("echo git stash", False),
    ("echo git stash pop", False),
    ("git commit -m 'git stash pop'", False),
]

PLUGIN_UPDATE_CORPUS = [
    ("claude plugin update cc-context", True),
    ("claude plugin update cc-context --scope user", True),
    ("claude plugin update --scope user cc-context", True),
    ("claude plugin update --scope=user cc-context", True),
    ("claude plugin update cc-context -s project", True),
    ("claude plugin update --json cc-context", True),
    ("claude plugin update cc-context --marketplace skills", True),
    ("claude plugin update a@b c", True),
    ("claude plugin update a@b c@d --scope user", False),
    ("claude plugin update cc-context && claude plugin update a@b", True),
    ("sh -c 'claude plugin update cc-context'", True),
    ("sudo claude plugin update cc-context", True),
    ("claude plugin update cc-context@skills", False),
    ("claude plugin update cc-context@skills --scope user", False),
    ("claude plugin update --scope user cc-context@skills", False),
    ("claude plugin update --marketplace skills cc-context@skills", False),
    ("claude plugin update", False),
    ("claude plugin update --scope local", False),
    ("claude plugin update --help", False),
    ("claude plugin list", False),
    ("claude plugin install cc-context", False),
    ("claude plugin list cc-context && claude plugin update a@b", False),
    ("claude --debug plugin update cc-context", False),
    ("echo claude plugin update cc-context", False),
    ("git status", False),
]

UNPINNED_UVX_CORPUS = [
    ("uvx capt-hook run PostToolUse", True),
    ("uvx captain-hook test", True),
    ("uvx cc-transcript list", True),
    ("uvx cc-notes status", True),
    ("uvx slop-cop check README.md", True),
    ("uvx cc-guides render", True),
    ("uvx cc-context repo overview", True),
    ("uvx --isolated capt-hook lint", True),
    ("uvx --from capt-hook capt-hook test", True),
    ("uvx --python 3.12 cc-guides render", True),
    ("uvx -p 3.12 cc-guides render", True),
    ("uvx --refresh capt-hook test", True),
    ("uvx --no-cache cc-transcript list", True),
    ("UV_EXCLUDE_NEWER=2026-01-01 uvx capt-hook test", True),
    ("sh -c 'uvx capt-hook test'", True),
    ("uvx capt-hook test && uvx cc-notes status", True),
    ("uvx capt-hook@9.19.0 test", False),
    ("uvx cc-notes@1.2.3 status", False),
    ("env -u UV_EXCLUDE_NEWER uvx capt-hook test", False),
    ("uvx ruff check", False),
    ("uvx --with foo ruff check", False),
    ("echo uvx capt-hook", False),
    ("uvx", False),
    ("git status", False),
]


@pytest.fixture
def commands_hooks() -> None:
    if (module := sys.modules.get(COMMANDS_MODULE)) is not None:
        importlib.reload(module)
    else:
        importlib.import_module(COMMANDS_MODULE)


def fires(command: str, tmp_path: Path) -> bool:
    evt = input_to_event(Event.PreToolUse, Input(command=command))
    return dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is not None


@pytest.mark.parametrize(("command", "blocked"), STASH_CORPUS, ids=[command for command, _ in STASH_CORPUS])
def test_git_stash_block_matches_the_hand_rolled_parser(
    commands_hooks: None, command: str, blocked: bool, tmp_path: Path
) -> None:
    assert fires(command, tmp_path) is blocked


@pytest.mark.parametrize(
    ("command", "warned"), PLUGIN_UPDATE_CORPUS, ids=[command for command, _ in PLUGIN_UPDATE_CORPUS]
)
def test_bare_plugin_update_nudge_matches_the_hand_rolled_parser(
    commands_hooks: None, command: str, warned: bool, tmp_path: Path
) -> None:
    assert fires(command, tmp_path) is warned


@pytest.mark.parametrize(
    ("command", "warned"), UNPINNED_UVX_CORPUS, ids=[command for command, _ in UNPINNED_UVX_CORPUS]
)
def test_unpinned_uvx_nudge_matches_the_hand_rolled_parser(
    commands_hooks: None, command: str, warned: bool, tmp_path: Path
) -> None:
    assert fires(command, tmp_path) is warned
