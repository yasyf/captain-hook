from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    Call,
    CommandSchema,
    Event,
    Input,
    LambdaCondition,
    Operand,
    Option,
    Or,
    PathMatches,
    RanCommand,
    Rewrite,
    Rewritten,
    Runs,
    Target,
    Tool,
    UsedSkill,
    Warn,
    hook,
    nudge,
    rewrite_command_occurrences,
)

if TYPE_CHECKING:
    from captain_hook import Arguments, BaseHookEvent, HookResult, Occurrence, PreToolUseEvent, WalkContext

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
FIND_TO_RG = CommandSchema(
    "find",
    operands=(Operand("root"),),
    options=(
        Option("follow", ("-L",), bool, prefix=True),
        Option("link_mode", ("-H", "-P"), bool, prefix=True),
        Option("name", ("-name",)),
        Option("iname", ("-iname",)),
        Option("type", ("-type",)),
        Option("print", ("-print",), bool),
    ),
    options_end_operands=True,
)
UNBOUNDED_ROOT = PathMatches(("/", "~", "/Users", "/Users/*", "**/.claude/worktrees"))
HOME_VARIABLE = re.compile(r"^\$\{?HOME\}?(?=/|$)")
GLOB_FLAGS = {"name": "--glob", "iname": "--iglob"}

STASH_READ_ONLY_VERBS = frozenset({"list", "show"})
STASH_ADDRESSED_VERBS = frozenset({"apply", "drop"})


def tagged_stash(args: tuple[str, ...]) -> bool:
    """Whether a push carries ``-m``/``--message``, so its entry stays findable by its own tag."""
    return any(
        arg.startswith("--message") or (not arg.startswith("--") and "m" in arg) for arg in args if arg.startswith("-")
    )


def safe_stash(args: tuple[str, ...]) -> bool:
    """Whether one ``git stash`` invocation reads the stack, names the entry it takes, or tags the entry it adds."""
    verb = args[0] if args and not args[0].startswith("-") else ""
    if verb in STASH_READ_ONLY_VERBS or "-h" in args or "--help" in args:
        return True
    if verb in STASH_ADDRESSED_VERBS:
        return any(not arg.startswith("-") for arg in args[1:])
    return verb in ("", "push") and tagged_stash(args)


def clobbers_the_shared_stash(evt: BaseHookEvent) -> bool:
    """Whether any ``git stash`` call can lose work: an untagged push, or one taking an entry blind."""
    return any(
        argv[:1] == ("stash",) and not safe_stash(argv[1:])
        for call in evt.cmd.calls("git")
        for argv in (call.verb_argv[1:],)
    )


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), LambdaCondition(clobbers_the_shared_stash)],
    message=(
        "BLOCKED: this git stash takes from the stack blind, and the stack is shared with every "
        "other worktree and session on the machine — a bare stash or a pop can swallow work that is "
        'not yours. Set work aside under a tag instead: `git stash push -u -m "<unique-tag>"`, then '
        "`git stash list --format='%H %gs'` to find your entry, `git stash apply <sha>` to restore it "
        "(never pop), and `git stash drop <n>` once you are done. In a jj repo you never need to "
        "stash — the working copy is commit @; use `jj new` to set it aside or `jj rebase` directly. "
        "In plain git, a WIP commit on a branch works too."
    ),
    block=True,
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash -u"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git stash pop --index stash@{0}"): Block(),
        Input(command="git stash push"): Block(),
        Input(command="git stash push -u"): Block(),
        Input(command="git stash clear"): Block(),
        Input(command="git stash apply"): Block(),
        Input(command="git stash drop"): Block(),
        Input(command="git -C /repo stash pop"): Block(),
        Input(command="git stash list && git stash pop"): Block(),
        Input(command="sh -c 'git stash pop'"): Block(),
        Input(command="git stash list"): Allow(),
        Input(command="git stash list --format='%H %gs'"): Allow(),
        Input(command="git stash show"): Allow(),
        Input(command="git stash show -p stash@{0}"): Allow(),
        Input(command="git stash --help"): Allow(),
        Input(command="git stash push -h"): Allow(),
        Input(command='git stash push -u -m "lane-stash-unblock"'): Allow(),
        Input(command='git stash push -um "lane-stash-unblock"'): Allow(),
        Input(command='git stash -u -m "lane-stash-unblock"'): Allow(),
        Input(command="git stash apply 0c4f3a1"): Allow(),
        Input(command="git stash apply --index stash@{1}"): Allow(),
        Input(command="git stash drop stash@{2}"): Allow(),
        Input(command="git stash drop -q 2"): Allow(),
        Input(command="git status"): Allow(),
        Input(command="echo git stash"): Allow(),
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

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), Or(Runs("pkill"), Runs("killall"))],
    message=(
        "BLOCKED: pkill and killall match every process on the machine whose name or command line "
        "fits the pattern, including other sessions' terminals, agents, and daemons. `pkill -f never` "
        "matched the word in every terminal's shell startup script and killed 15 unrelated Claude "
        "sessions (user 2026-09-24: 'Block pkill -f / killall'). Kill only processes you started, by "
        "pid: `kill <pid>` with pids from `$!`, `pgrep -P <your-pid>`, or `lsof -t -a -d cwd +D <dir>`, "
        "after checking each with `ps -o pid,command -p <pid>`."
    ),
    block=True,
    tests={
        Input(command='pkill -f "never" 2>/dev/null; codex-ask --help'): Block(),
        Input(command="pkill node"): Block(),
        Input(command="pkill -9 -f 'vite dev'"): Block(),
        Input(command="killall claude"): Block(),
        Input(command="sudo pkill -f server"): Block(),
        Input(command="kill 12345"): Allow(),
        Input(command="pgrep -f never"): Allow(),
        Input(command="echo pkill -f never"): Allow(),
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


def operands(call: Call, *, after: int = 0) -> list[str]:
    """The call's positional words past ``after``, with flags and their values dropped."""
    rest: list[str] = []
    skip = False
    for arg in call.args[after:]:
        if skip:
            skip = False
        elif arg in ("--scope", "--marketplace"):
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


def runs_an_unpinned_fleet_tool(evt: BaseHookEvent) -> bool:
    for call in evt.command.calls("uvx"):
        if "UV_EXCLUDE_NEWER" in call.source.args:
            continue
        if any(
            name in {"capt-hook", "captain-hook", "cc-transcript", "cc-notes", "slop-cop", "cc-guides", "cc-context"}
            for name in operands(call)
        ):
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


def home_respelled(target: Target) -> Target:
    """A ``$HOME``-rooted target respelled with ``~``, since ``PathMatches`` expands no variables."""
    if target.value is not None or not HOME_VARIABLE.match(raw := target.raw.strip("\"'")):
        return target
    return Target(text := HOME_VARIABLE.sub("~", raw), text, target.cwd)


def unbounded_root(arguments: Arguments) -> bool:
    """Whether a search rooted here walks the whole volume: /, a home directory, or the worktree pool."""
    return any(UNBOUNDED_ROOT(home_respelled(target)) for target in arguments.paths("root"))


def rg_equivalent(call: Call) -> str | None:
    """``rg --files`` spelling one ``find`` invocation, or None when the shapes do not correspond."""
    arguments = FIND_TO_RG.bind(call)
    if not arguments.complete or arguments.values.get("type", ("f",)) != ("f",) or not unbounded_root(arguments):
        return None
    match [(glob, words) for role, glob in GLOB_FLAGS.items() if (words := arguments.words.get(role))]:
        case [(glob, (pattern,))]:
            return " ".join(
                (
                    "rg --files",
                    *(("-L",) if arguments.values.get("follow") else ()),
                    arguments.words["root"][0].raw,
                    glob,
                    pattern.raw,
                )
            )
        case _:
            return None


def guard_find(evt: PreToolUseEvent, occ: Occurrence, ctx: WalkContext) -> str | Rewritten | HookResult | None:
    command = occ.command.unwrapped
    if command.executable != "find":
        return None
    if "-exec" in command.args or "-execdir" in command.args:
        return evt.block(FIND_EXEC_BLOCKED)
    if not ctx.spliceable or command.env or command.redirects:
        return None
    rewritten = rg_equivalent(Call(evt.cmd, occ, ctx.cwd))
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
        Input(command="find ~ -iname '*.pem' -type f"): Rewrite(pattern="rg --files ~ --iglob '*.pem'"),
        Input(command="find -L ~ -iname '*.pem'"): Rewrite(pattern="rg --files -L ~ --iglob '*.pem'"),
        Input(command="find ~ -name x -print"): Rewrite(pattern="rg --files ~ --glob x"),
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
        Input(command="find ~ -iname foo -newer bar"): Allow(),
        Input(command="find ~ -name x -print0"): Allow(),
        Input(command="find ~ -iname a -iname b"): Allow(),
        # -exec blocks at any root, and beats the rewrite.
        Input(command=r"find . -name '*.pyc' -exec rm {} \;"): Block(),
        Input(command="find ~ -type f -exec grep -l foo {} +"): Block(),
        Input(command="find /Users/yasyf -iname '*.log' -exec rm {} +"): Block(),
        # The word `find` as an argument is not a find call.
        Input(command="echo find ~ -iname foo"): Allow(),
        Input(command="git status"): Allow(),
    },
)
