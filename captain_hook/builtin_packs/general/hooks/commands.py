from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    CwdHasFiles,
    Event,
    Input,
    Or,
    RanCommand,
    Runs,
    Tool,
    UsedSkill,
    Warn,
    hook,
    nudge,
)
from captain_hook.types import Command

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


nudge(
    "`claude plugin update <name>` with a bare name fails 'not found', and the scope silently "
    "defaults to user regardless of cwd. Take the qualifier and scope from the plugin's "
    "~/.claude/plugins/installed_plugins.json record and pass them: "
    "`claude plugin update <plugin>@<marketplace> --scope <scope>`.",
    only_if=[Tool("Bash"), Command(r"\bclaude\s+plugin\s+update\s+\S")],
    skip_if=[Command(r"plugin\s+update\b[^;&|]*@")],
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


nudge(
    "Right after a release, a bare `uvx` can serve a stale cached tool env, and the "
    "UV_EXCLUDE_NEWER window hides just-published versions. When you need the fresh release, pin "
    "exact (`uvx <pkg>@X.Y.Z`) or prefix `env -u UV_EXCLUDE_NEWER`.",
    only_if=[
        Tool("Bash"),
        Command(r"\buvx\b"),
        Command(r"\b(?:capt-hook|captain-hook|cc-transcript|cc-notes|slop-cop|cc-guides|cc-context)\b"),
    ],
    skip_if=[
        Command(r"(?:capt-hook|captain-hook|cc-transcript|cc-notes|slop-cop|cc-guides|cc-context)@"),
        Command(r"\benv\s+-u\s+UV_EXCLUDE_NEWER\b"),
    ],
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
    only_if=[Tool("Bash"), Runs("uv", "sync"), CwdHasFiles("Cargo.toml", "pyproject.toml")],
    skip_if=[Command(r"--reinstall-package")],
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
