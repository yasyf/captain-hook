from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    Call,
    CommandMatches,
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
    T,
    Target,
    Tool,
    UsedSkill,
    Warn,
    hook,
    nudge,
    rewrite_command_occurrences,
)
from captain_hook.cmd import COMMAND_VALUE_FLAGS

if TYPE_CHECKING:
    from captain_hook import Arguments, BaseHookEvent, Occurrence, PreToolUseEvent, WalkContext

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
HOME_SPELLINGS = ("$HOME", "${HOME}")
GLOB_FLAGS = {"name": "--glob", "iname": "--iglob"}
FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir"})
ATTEMPT_TOOLS = "Bash|Edit|Write"

GIT_STASH = CommandSchema(
    "git",
    operands=(Operand("subcommand"), Operand("verb"), Operand("targets", count="*")),
    options=(
        Option("environment", COMMAND_VALUE_FLAGS["git"]),
        Option("message", ("-m", "--message", *(f"-{letter}m" for letter in "pkuaqS"))),
        Option("pathspec_file", ("--pathspec-from-file",)),
        Option("help", ("-h", "--help"), bool),
        Option(
            "inert",
            (
                "-p",
                "-P",
                "-k",
                "-u",
                "-a",
                "-q",
                "-S",
                "--paginate",
                "--no-pager",
                "--bare",
                "--no-optional-locks",
                "--no-replace-objects",
                "--no-advice",
                "--no-lazy-fetch",
                "--literal-pathspecs",
                "--glob-pathspecs",
                "--noglob-pathspecs",
                "--icase-pathspecs",
                "--patch",
                "--keep-index",
                "--no-keep-index",
                "--include-untracked",
                "--no-include-untracked",
                "--all",
                "--quiet",
                "--staged",
                "--index",
                "--pathspec-file-nul",
            ),
            bool,
        ),
    ),
)
CLAUDE_PLUGIN_UPDATE = CommandSchema(
    "claude",
    operands=(Operand("group"), Operand("action"), Operand("plugins", count="*")),
    options=(
        Option("scope", ("-s", "--scope")),
        Option("marketplace", ("--marketplace",)),
        Option("accept_command", ("--accept-command",)),
        Option("flag", ("-h", "--help", "-y", "--yes", "--json"), bool),
    ),
)
FLEET_TOOLS = frozenset(
    {"capt-hook", "captain-hook", "cc-transcript", "cc-notes", "slop-cop", "cc-guides", "cc-context"}
)
UVX = CommandSchema(
    "uvx",
    operands=(Operand("tool"), Operand("args", count="*")),
    operands_end_options=True,
    options=(
        Option(
            "value",
            (
                "--from",
                "-w",
                "--with",
                "--with-editable",
                "--with-requirements",
                "-p",
                "--python",
                "-c",
                "--constraints",
                "--overrides",
                "--env-file",
                "--index",
                "--default-index",
                "-i",
                "--index-url",
                "--extra-index-url",
                "-f",
                "--find-links",
                "--index-strategy",
                "--exclude-newer",
                "--directory",
            ),
        ),
        Option(
            "flag",
            (
                "--isolated",
                "-n",
                "--no-cache",
                "--refresh",
                "--offline",
                "-q",
                "--quiet",
                "-v",
                "--verbose",
                "--no-index",
            ),
            bool,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class OperandIs:
    name: str
    values: Collection[str]

    def __call__(self, arguments: Arguments) -> bool:
        bound = arguments.values.get(self.name, ())
        return (bound[0] if bound else "") in self.values


@dataclass(frozen=True, slots=True)
class Binds:
    name: str

    def __call__(self, arguments: Arguments) -> bool:
        return bool(arguments.values.get(self.name))


@dataclass(frozen=True, slots=True)
class AllOf:
    predicates: tuple[Callable[[Arguments], bool], ...]

    def __init__(self, *predicates: Callable[[Arguments], bool]) -> None:
        object.__setattr__(self, "predicates", predicates)

    def __call__(self, arguments: Arguments) -> bool:
        return all(predicate(arguments) for predicate in self.predicates)


def names_an_entry(arguments: Arguments) -> bool:
    """Whether the verb names its entry; a ``$(...)`` operand leaves operands incomplete with nothing unread."""
    return bool(arguments.values.get("targets")) or not (arguments.operands_complete or arguments.unread)


def names_a_bare_plugin(arguments: Arguments) -> bool:
    return any("@" not in (name or "") for name in arguments.values.get("plugins", ()))


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        CommandMatches(
            GIT_STASH,
            only_if=(OperandIs("subcommand", {"stash"}),),
            skip_if=(
                Binds("help"),
                OperandIs("verb", {"list", "show"}),
                AllOf(OperandIs("verb", {"apply", "drop"}), names_an_entry),
                AllOf(OperandIs("verb", {"", "push"}), Binds("message")),
            ),
        ),
    ],
    message=(
        "This `git stash` takes from or adds to the stack shared by every worktree without a tag. "
        'Run `git stash push -u -m "<unique-tag>"` to set work aside, then `git stash apply <sha>` (never pop).'
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
        Input(command="git stash drop -q $(git stash list | grep ccx-followups-test | cut -d: -f1)"): Allow(),
        Input(command="git stash apply -q $(git stash list --format='%H %gs' | grep tag | cut -d' ' -f1)"): Allow(),
        Input(command="git stash pop $(git stash list | grep tag | cut -d: -f1)"): Block(),
        Input(command="git stash drop --bogus"): Block(),
        Input(command="git stash list | grep tag"): Allow(),
        Input(command="git -C /repo stash list | head"): Allow(),
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
        "`jj op restore` and `jj undo` rewrite the whole repo to an earlier operation and can clobber "
        "everything since. Run `jj op log` to find the operation, then `jj --at-op=<op> <read command>` to inspect it."
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

nudge(
    "Two tool failures without a second opinion. Run `/codex` before a third attempt.",
    only_if=[Tool(ATTEMPT_TOOLS)],
    skip_if=[UsedSkill("codex"), RanCommand("codex")],
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.tool_calls.named(ATTEMPT_TOOLS).failed().count() >= 2,
    tests={
        Input(
            command="uv run pytest",
            error="ModuleNotFoundError",
            transcript=[
                *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest"),
                *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest -x"),
            ],
        ): Warn(pattern="/codex"),
        Input(
            tool="Edit",
            file="m.py",
            error="String to replace not found in file.",
            transcript=[
                *T.tool_turn("Bash", result="SyntaxError", is_error=True, command="uv run pytest"),
                *T.tool_turn("Edit", result="String to replace not found", is_error=True, file_path="m.py"),
            ],
        ): Warn(pattern="/codex"),
        Input(
            command="uv run pytest",
            error="ModuleNotFoundError",
            transcript=T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest"),
        ): Allow(),
        Input(
            command="uv run pytest",
            error="ModuleNotFoundError",
            transcript=[
                *T.tool_turn("Grep", result="Path does not exist: api/src/foo", is_error=True, pattern="x"),
                *T.tool_turn("Agent", result="Concurrent subagent limit reached", is_error=True, prompt="go"),
                *T.tool_turn("mcp__datadog__search_logs", result="MCP error -32603: timeout", is_error=True, query="x"),
                *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest"),
            ],
        ): Allow(),
        Input(
            tool="mcp__plugin_cc-context_cc-context__ccx_code_grep",
            tool_input={"pattern": "x", "paths": ["api/src/missing"]},
            error="path not found: api/src/missing",
            transcript=[
                *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest"),
                *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest -x"),
            ],
        ): Allow(),
    },
)

nudge(
    "Update plugins by qualified name and scope from `~/.claude/plugins/installed_plugins.json`. "
    "Run `claude plugin update <plugin>@<marketplace> --scope <scope>`.",
    only_if=[
        Tool("Bash"),
        CommandMatches(
            CLAUDE_PLUGIN_UPDATE,
            only_if=(OperandIs("group", {"plugin"}), OperandIs("action", {"update"}), names_a_bare_plugin),
        ),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="claude plugin update cc-context"): Warn(),
        Input(command="claude plugin update cc-context --scope user"): Warn(),
        Input(command="claude plugin update cc-context@cc-context --scope user"): Allow(),
        Input(command="claude plugin update cc-context@cc-context"): Allow(),
        Input(command="claude plugin list"): Allow(),
        Input(command="git status"): Allow(),
    },
)

nudge(
    "A bare `uvx` of a fleet tool can serve a stale cached env right after a release. "
    "Run `env -u UV_EXCLUDE_NEWER uvx <pkg> <args>` to get the fresh one.",
    only_if=[Tool("Bash"), CommandMatches(UVX, only_if=(OperandIs("tool", FLEET_TOOLS),))],
    skip_if=[Runs("env", "-u", "UV_EXCLUDE_NEWER")],
    events=Event.PreToolUse,
    tests={
        Input(command="uvx capt-hook run PostToolUse"): Warn(),
        Input(command="uvx cc-notes status"): Warn(),
        Input(command="uvx capt-hook@9.19.0 test"): Allow(),
        Input(command="env -u UV_EXCLUDE_NEWER uvx capt-hook test"): Allow(),
        Input(command="uvx ruff check"): Allow(),
    },
)


def home_respelled(target: Target) -> Target:
    """A ``$HOME``-rooted target respelled with ``~``, since ``PathMatches`` expands no variables."""
    if target.value is not None:
        return target
    head, slash, rest = target.raw.strip("\"'").partition("/")
    return Target(text := f"~{slash}{rest}", text, target.cwd) if head in HOME_SPELLINGS else target


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


def execs_per_hit(evt: BaseHookEvent) -> bool:
    return any(not FIND_EXEC_FLAGS.isdisjoint(call.args) for call in evt.cmd.calls("find"))


def rg_for_unbounded_find(evt: PreToolUseEvent, occ: Occurrence, ctx: WalkContext) -> Rewritten | None:
    command = occ.command.unwrapped
    if command.executable != "find" or not ctx.spliceable or command.env or command.redirects:
        return None
    rewritten = rg_equivalent(Call(evt.cmd, occ, ctx.cwd))
    return (
        Rewritten(
            rewritten,
            "Rewrote an unbounded `find` to `rg --files`, which honours .gitignore and skips hidden files. "
            "Re-run with `rg --files -uu` to include the ignored and hidden ones.",
        )
        if rewritten
        else None
    )


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), LambdaCondition(execs_per_hit)],
    message=(
        "`find -exec` forks one process per hit and walks ignored trees. "
        "Run `fd -H -i '<pattern>' <root> -x <cmd>` instead."
    ),
    block=True,
    tests={
        Input(command=r"find . -name '*.pyc' -exec rm {} \;"): Block(),
        Input(command="find ~ -type f -exec grep -l foo {} +"): Block(),
        Input(command="find /Users/yasyf -iname '*.log' -exec rm {} +"): Block(),
        Input(command="find ~ -type f -execdir ls {} +"): Block(),
        Input(command="find . -name '*.pyc'"): Allow(),
        Input(command="echo find . -exec rm {} +"): Allow(),
        Input(command="git status"): Allow(),
    },
)

rewrite_command_occurrences(
    visit=rg_for_unbounded_find,
    tests={
        Input(command="find /Users/yasyf -iname '*plugin-workspace-tools*'"): Rewrite(
            pattern="rg --files /Users/yasyf --iglob '*plugin-workspace-tools*'"
        ),
        Input(command="find ~ -iname '*.pem'"): Rewrite(pattern="rg --files ~ --iglob '*.pem'"),
        Input(command="find $HOME -type f -iname foo"): Rewrite(pattern="rg --files $HOME --iglob foo"),
        Input(command="find ~ -iname '*.pem' -type f"): Rewrite(pattern="rg --files ~ --iglob '*.pem'"),
        Input(command="find -L ~ -iname '*.pem'"): Rewrite(pattern="rg --files -L ~ --iglob '*.pem'"),
        Input(command="find ~ -name x -print"): Rewrite(pattern="rg --files ~ --glob x"),
        Input(command="find /Users/yasyf -name '*.p12'"): Rewrite(pattern="rg --files /Users/yasyf --glob '*.p12'"),
        Input(command="find ~/.claude/worktrees -iname '*.lock'"): Rewrite(
            pattern="rg --files ~/.claude/worktrees --iglob '*.lock'"
        ),
        Input(command="find / -iname 'libssl*' | head -5"): Rewrite(pattern="rg --files / --iglob 'libssl*' | head -5"),
        Input(command="cd /tmp && find ~ -name Cargo.toml"): Rewrite(
            pattern="cd /tmp && rg --files ~ --glob Cargo.toml"
        ),
        Input(command="find . -iname '*.ts'"): Allow(),
        Input(command="find ~/Code/monorepo -iname '*.ts'"): Allow(),
        Input(command="find api/src -iname '*.ts'"): Allow(),
        Input(command="find ~ -type d -iname build"): Allow(),
        Input(command="find ~ -iname foo -newer bar"): Allow(),
        Input(command="find ~ -name x -print0"): Allow(),
        Input(command="find ~ -iname a -iname b"): Allow(),
        Input(command="echo find ~ -iname foo"): Allow(),
        Input(command="git status"): Allow(),
    },
)
