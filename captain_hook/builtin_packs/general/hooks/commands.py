from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    CwdHasFiles,
    Event,
    Input,
    LambdaCondition,
    Or,
    RanCommand,
    Rewrite,
    Rewritten,
    Runs,
    Tool,
    UsedSkill,
    Warn,
    hook,
    nudge,
    rewrite_command_occurrences,
)

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent, Call, Command, HookResult, Occurrence, PreToolUseEvent, WalkContext

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), Runs("git", "stash")],
    message=(
        "BLOCKED: git stash is not allowed. In a jj repo you never need to stash — the working copy "
        "is commit @; use `jj new` to set it aside or `jj rebase` directly (no clean tree required). "
        "In plain git, commit your changes to a branch."
    ),
    block=True,
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
    },
)

hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(Runs("jj", "op", "restore"), Runs("jj", "operation", "restore"), Runs("jj", "undo")),
    ],
    message=(
        "BLOCKED: jj op restore and jj undo rewrite the whole repo to an earlier operation and can "
        "clobber everything since. Inspect instead: `jj op log` to find the operation, `jj op show <op>` "
        "or `jj op diff --op <op>` to see what it changed, and any read command against that state via "
        "`jj --at-op=<op> ...` (e.g. `jj --at-op=<op> st`, `jj --at-op=<op> file show -r <rev> <path>`). "
        "To recover content without time-travel: `jj restore --from <commit> <path>` for one file (hidden "
        "commits stay addressable by full ID via `jj --at-op=<op> log`), or materialize the old state in "
        "a throwaway workspace: `jj --at-op=<op> workspace add <dir> -r <rev>`. If a true restore is "
        "needed, stop and ask the user to run it."
    ),
    block=True,
    tests={
        Input(command="jj op restore abc123"): Block(),
        Input(command="jj operation restore abc123"): Block(),
        Input(command="jj undo"): Block(),
        Input(command="jj op log"): Allow(),
        Input(command="jj op show"): Allow(),
        Input(command="jj --at-op=abc123 log"): Allow(),
    },
)


# Requires the codex plugin (/plugin install codex@skills from yasyf/cc-skills).
# Delete this nudge if you don't use Codex.
nudge(
    """
    Multiple tool failures detected without a /codex invocation. After 2 failed
    approaches, get a second opinion from `/codex` before attempting a 3rd —
    Codex catches errors that Claude may miss.
    """,
    skip_if=[UsedSkill("codex"), RanCommand("codex")],
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 2,
)


VALUE_FLAGS = ("--scope", "--marketplace")


def operands(call: Call, *, after: int = 0) -> list[str]:
    """The call's positional words past ``after``, with flags and their values dropped."""
    rest: list[str] = []
    skip = False
    for arg in call.args[after:]:
        if skip:
            skip = False
        elif arg in VALUE_FLAGS:
            skip = True
        elif not arg.startswith("-"):
            rest.append(arg)
    return rest


def updates_a_bare_plugin(evt: BaseHookEvent) -> bool:
    return any(
        call.args[:2] == ("plugin", "update") and any("@" not in name for name in operands(call, after=2))
        for call in evt.command.calls("claude")
    )


nudge(
    "`claude plugin update <name>` with a bare name fails 'not found', and the scope silently "
    "defaults to user regardless of cwd. Take the qualifier and scope from the plugin's "
    "~/.claude/plugins/installed_plugins.json record and pass them: "
    "`claude plugin update <plugin>@<marketplace> --scope <scope>`.",
    only_if=[Tool("Bash"), LambdaCondition(updates_a_bare_plugin)],
    events=Event.PreToolUse,
    tests={
        # Bare name -> warn (the qualifier and scope are missing).
        Input(command="claude plugin update cc-context"): Warn(),
        # A --scope without an @ qualifier is still a bare name -> warn.
        Input(command="claude plugin update cc-context --scope user"): Warn(),
        # Fully qualified with scope -> silent.
        Input(command="claude plugin update cc-context@cc-context --scope user"): Allow(),
        # Qualified without scope -> silent (the @ carve-out).
        Input(command="claude plugin update cc-context@cc-context"): Allow(),
        # Not a plugin update -> silent.
        Input(command="claude plugin list"): Allow(),
        Input(command="git status"): Allow(),
    },
)


FLEET_TOOLS = frozenset(
    {"capt-hook", "captain-hook", "cc-transcript", "cc-notes", "slop-cop", "cc-guides", "cc-context"}
)


def runs_an_unpinned_fleet_tool(evt: BaseHookEvent) -> bool:
    for call in evt.command.calls("uvx"):
        if "UV_EXCLUDE_NEWER" in call.source.args:
            continue
        if any(name in FLEET_TOOLS for name in operands(call)):
            return True
    return False


nudge(
    "Right after a release, a bare `uvx` can serve a stale cached tool env, and the "
    "UV_EXCLUDE_NEWER window hides just-published versions. When you need the fresh release, pin "
    "exact (`uvx <pkg>@X.Y.Z`) or prefix `env -u UV_EXCLUDE_NEWER`.",
    only_if=[Tool("Bash"), LambdaCondition(runs_an_unpinned_fleet_tool)],
    events=Event.PreToolUse,
    tests={
        # Bare, unpinned uvx of a fleet tool -> warn.
        Input(command="uvx capt-hook run PostToolUse"): Warn(),
        Input(command="uvx cc-notes status"): Warn(),
        # Version-pinned -> silent.
        Input(command="uvx capt-hook@9.19.0 test"): Allow(),
        # env -u UV_EXCLUDE_NEWER prefix -> silent.
        Input(command="env -u UV_EXCLUDE_NEWER uvx capt-hook test"): Allow(),
        # A non-fleet tool -> silent (scoped narrowly to avoid noise).
        Input(command="uvx ruff check"): Allow(),
    },
)


nudge(
    "`uv sync` silently no-ops on Rust-only changes in a maturin project — the native extension is "
    "not rebuilt, so you keep running the stale binary. Force the rebuild with "
    "`uv sync --reinstall-package <name>` (the distribution whose Rust you changed).",
    only_if=[
        Tool("Bash"),
        Runs("uv", "sync"),
        CwdHasFiles("Cargo.toml", "pyproject.toml"),
    ],
    skip_if=[LambdaCondition(lambda evt: any("--reinstall-package" in call.flags for call in evt.command.calls("uv")))],
    events=Event.PreToolUse,
    # Fire path (markers present) is covered by the CwdHasFiles unit test — the inline fixture can't stage marker files.
    tests={
        # --reinstall-package present -> silent.
        Input(command="uv sync --reinstall-package captain-hook"): Allow(),
        # No maturin markers at cwd -> silent (the CwdHasFiles gate).
        Input(command="uv sync"): Allow(),
        # Not uv sync -> silent.
        Input(command="git status"): Allow(),
    },
)


NAME_TEST_TO_GLOB = {"-name": "--glob", "-iname": "--iglob"}
FIND_TO_RG_NOTE = (
    "Rewrote an unbounded `find` to `rg --files`: a name search rooted at $HOME or / walks every "
    "worktree, node_modules, and build tree on the volume and pins a core for minutes. rg honours "
    ".gitignore/.ignore and skips hidden files, so it returns the tracked hits in well under a "
    "second. Need the ignored or hidden ones too? Re-run with `rg --files -uu` or `fd -H -I`."
)
FIND_EXEC_BLOCKED = (
    "BLOCKED: `find -exec` forks one process per hit and traverses ignored trees, so it pins a core "
    "for minutes on any real checkout. Use `fd` instead: `fd -H -i '<pattern>' <root> -x <cmd>` runs "
    "the same command per hit, in parallel, honouring .gitignore/.ignore — and `-X` batches them "
    "into one invocation like `-exec {} +`. To act on content matches rather than names, "
    "`rg -l '<pattern>' | xargs <cmd>`."
)


def unbounded_root(value: str) -> bool:
    """Whether a search rooted here walks the whole volume: /, $HOME, /Users, or the worktree pool."""
    path = value.rstrip("/") or "/"
    if path == "$HOME" or path.startswith("$HOME/"):
        path = "~" + path[len("$HOME") :]
    if path.endswith("/.claude/worktrees"):
        return True
    if path in ("/", "~", "/Users"):
        return True
    parts = PurePosixPath(path).parts
    return len(parts) == 3 and parts[:2] == ("/", "Users")


def rg_equivalent(command: Command) -> str | None:
    """``rg --files`` spelling one ``find`` invocation, or None when the shapes do not correspond."""
    words = command.words[1:]
    if not words or not unbounded_root(command.args[0]):
        return None
    rest = list(zip(command.args[1:], words[1:], strict=True))
    if rest[:1] and rest[0][0] == "-type":
        if len(rest) < 2 or rest[1][0] != "f":
            return None
        rest = rest[2:]
    if len(rest) != 2 or (glob := NAME_TEST_TO_GLOB.get(rest[0][0])) is None:
        return None
    return f"rg --files {words[0].raw} {glob} {rest[1][1].raw}"


def guard_find(evt: PreToolUseEvent, occ: Occurrence, ctx: WalkContext) -> str | Rewritten | HookResult | None:
    command = occ.command.unwrapped
    if command.executable != "find":
        return None
    if "-exec" in command.args or "-execdir" in command.args:
        return evt.block(FIND_EXEC_BLOCKED)
    if not ctx.spliceable or command.env or command.redirects:
        return None
    rewritten = rg_equivalent(command)
    return Rewritten(rewritten, FIND_TO_RG_NOTE) if rewritten else None


rewrite_command_occurrences(
    visit=guard_find,
    tests={
        Input(command="find /Users/yasyf -iname '*plugin-workspace-tools*'"): Rewrite(
            pattern="rg --files /Users/yasyf --iglob '*plugin-workspace-tools*'"
        ),
        # The root keeps its source spelling, so ~ still expands and the glob stays quoted.
        Input(command="find ~ -iname '*.pem'"): Rewrite(pattern="rg --files ~ --iglob '*.pem'"),
        Input(command="find $HOME -type f -iname foo"): Rewrite(pattern="rg --files $HOME --iglob foo"),
        Input(command="find /Users/yasyf -name '*.p12'"): Rewrite(pattern="rg --files /Users/yasyf --glob '*.p12'"),
        Input(command="find ~/.claude/worktrees -iname '*.lock'"): Rewrite(
            pattern="rg --files ~/.claude/worktrees --iglob '*.lock'"
        ),
        # Only the find segment is spliced; its siblings survive byte-for-byte.
        Input(command="find / -iname 'libssl*' | head -5"): Rewrite(pattern="rg --files / --iglob 'libssl*' | head -5"),
        Input(command="cd /tmp && find ~ -name Cargo.toml"): Rewrite(
            pattern="cd /tmp && rg --files ~ --glob Cargo.toml"
        ),
        # A scoped root is cheap -> untouched.
        Input(command="find . -iname '*.ts'"): Allow(),
        Input(command="find ~/Code/monorepo -iname '*.ts'"): Allow(),
        Input(command="find api/src -iname '*.ts'"): Allow(),
        # -type d has no rg --files equivalent -> untouched.
        Input(command="find ~ -type d -iname build"): Allow(),
        # -exec blocks at any root, and beats the rewrite.
        Input(command=r"find . -name '*.pyc' -exec rm {} \;"): Block(),
        Input(command="find ~ -type f -exec grep -l foo {} +"): Block(),
        Input(command="find /Users/yasyf -iname '*.log' -exec rm {} +"): Block(),
        # The word `find` as an argument is not a find call.
        Input(command="echo find ~ -iname foo"): Allow(),
        Input(command="git status"): Allow(),
    },
)
