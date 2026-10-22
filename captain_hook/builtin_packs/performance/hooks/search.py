from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    CommandMatches,
    CommandSchema,
    Event,
    Input,
    Operand,
    Option,
    Or,
    PathMatches,
    Target,
    Targets,
    Tool,
    hook,
)
from captain_hook.command_schema import glob_prefix
from captain_hook.util.scratch import TEMP_ROOTS
from captain_hook.util.vcs import is_repo_root

if TYPE_CHECKING:
    from collections.abc import Iterable

    from captain_hook import Arguments
    from captain_hook.command_schema import Scalar

BROAD_ROOTS = PathMatches(
    (
        "/",
        "~",
        "/Users",
        "/Users/*",
        "/home",
        "/home/*",
        "/System/Volumes/Data",
        "**/.claude/worktrees",
        "**/.claude/worktrees/*",
        "**/.orca/workspaces",
        "**/.orca/workspaces/*",
        "**/.claude/scratch",
        "**/.claude/scratch/*",
        *(pattern for root in TEMP_ROOTS for pattern in (str(root), f"{root}/*")),
    ),
    unresolved=True,
)
PATTERN_ROLES = ("patterns", "pattern_files", "query")
RECURSIVE_ROLES = ("recursive", "depth", "include_dir")
INFO_ROLES = ("info", "listing", "generate")
RECURSE_ACTIONS = frozenset({"rec", "recu", "recur", "recurs", "recurse", "dereference-recurse"})
STDIN_REDIRECTS = frozenset({"<", "<<<"})

GREP = CommandSchema(
    "grep",
    operands=(Operand("operands", count="*"),),
    options=(
        Option("patterns", ("-e", "--regexp")),
        Option("pattern_files", ("-f", "--file")),
        Option("recursive", ("-r", "-R", "--recursive", "--dereference-recursive"), bool),
        Option("directories", ("-d", "--directories")),
        Option("include", ("--include",)),
        Option("include_dir", ("--include-dir",)),
        Option("exclude", ("--exclude", "--exclude-dir", "--exclude-from")),
        Option("info", ("-V", "--version", "--help"), bool),
        Option(
            "parameter",
            (
                "-A",
                "-B",
                "-C",
                "-D",
                "-m",
                "--after-context",
                "--before-context",
                "--context",
                "--devices",
                "--max-count",
                "--binary-files",
                "--label",
                "--group-separator",
            ),
        ),
        Option(
            "mode",
            (
                *(f"-{digit}" for digit in range(10)),
                *(f"-{letter}" for letter in "EFGHIJLMOPSTUXZabchilnopqsuvwxyz"),
                "--basic-regexp",
                "--extended-regexp",
                "--fixed-strings",
                "--fixed-regexp",
                "--perl-regexp",
                "--byte-offset",
                "--color",
                "--colour",
                "--count",
                "--files-with-matches",
                "--files-without-match",
                "--ignore-case",
                "--no-ignore-case",
                "--initial-tab",
                "--line-buffered",
                "--line-number",
                "--line-regexp",
                "--no-filename",
                "--with-filename",
                "--no-messages",
                "--null",
                "--null-data",
                "--only-matching",
                "--quiet",
                "--silent",
                "--invert-match",
                "--text",
                "--binary",
                "--unix-byte-offsets",
                "--word-regexp",
                "--no-group-separator",
                "--mmap",
                "--bz2decompress",
                "--lzma",
                "--xz",
                "--decompress",
            ),
            bool,
        ),
    ),
)

UGREP = CommandSchema(
    "ugrep",
    operands=(Operand("operands", count="*"),),
    options=(
        Option("patterns", ("-e", "--regexp", "-N", "--neg-regexp", "--and", "--andnot", "--not")),
        Option("pattern_files", ("-f", "--file")),
        Option("query", ("-Q", "--query"), bool),
        Option(
            "recursive",
            ("-r", "-R", "--recursive", "--dereference-recursive", "--index", *(f"-{depth}" for depth in range(1, 10))),
            bool,
        ),
        Option("depth", ("--depth",)),
        Option("directories", ("-d", "--directories")),
        Option("glob", ("-g", "--glob", "--iglob", "--include")),
        Option("include_dir", ("--include-dir",)),
        Option("include_from", ("--include-from",)),
        Option("extension", ("-O", "--file-extension")),
        Option("type", ("-t", "--file-type")),
        Option("magic", ("-M", "--file-magic")),
        Option("exclude", ("--exclude", "--exclude-dir", "--exclude-from")),
        Option("from", ("--from",)),
        Option("info", ("-V", "--version", "--help", "-?"), bool),
        Option(
            "parameter",
            (
                "-A",
                "-B",
                "-C",
                "-D",
                "-J",
                "-K",
                "-m",
                "--after-context",
                "--before-context",
                "--context",
                "--devices",
                "--jobs",
                "--range",
                "--min-line",
                "--max-line",
                "--min-count",
                "--max-count",
                "--max-files",
                "--max-size",
                "--min-size",
                "--binary-files",
                "--colors",
                "--colours",
                "--context-separator",
                "--delay",
                "--encoding",
                "--exclude-fs",
                "--include-fs",
                "--filter",
                "--filter-magic-label",
                "--format",
                "--label",
                "--replace",
                "--zmax",
            ),
        ),
        Option(
            "mode",
            (
                *(f"-{letter}" for letter in "0EFGHILPSTUWXYZabchijklnopqsuvwxyz%@.+^"),
                "--text",
                "--all",
                "--bool",
                "--break",
                "--byte-offset",
                "--count",
                "--color",
                "--colour",
                "--config",
                "--cpp",
                "--csv",
                "--dotall",
                "--extended-regexp",
                "--fixed-strings",
                "--files",
                "--free-space",
                "--basic-regexp",
                "--glob-ignore-case",
                "--group-separator",
                "--with-filename",
                "--heading",
                "--hexdump",
                "--hidden",
                "--hyperlink",
                "--ignore-binary",
                "--ignore-case",
                "--ignore-files",
                "--smart-case",
                "--json",
                "--column-number",
                "--files-without-match",
                "--files-with-matches",
                "--line-buffered",
                "--lines",
                "--match",
                "--mmap",
                "--line-number",
                "--null",
                "--null-data",
                "--only-matching",
                "--only-line-number",
                "--perl-regexp",
                "--pager",
                "--pretty",
                "--quiet",
                "--silent",
                "--dereference-files",
                "--save-config",
                "--separator",
                "--split",
                "--sort",
                "--stats",
                "--initial-tab",
                "--tabs",
                "--tag",
                "--tree",
                "--ascii",
                "--binary",
                "--ungroup",
                "--invert-match",
                "--view",
                "--with-hex",
                "--word-regexp",
                "--width",
                "--hex",
                "--line-regexp",
                "--xml",
                "--empty",
                "--any-line",
                "--passthru",
                "--fuzzy",
                "--decompress",
                "--no-any-line",
                "--no-ascii",
                "--no-binary",
                "--no-bool",
                "--no-break",
                "--no-byte-offset",
                "--no-color",
                "--no-config",
                "--no-confirm",
                "--no-count",
                "--no-decompress",
                "--no-dereference",
                "--no-dereference-files",
                "--no-dotall",
                "--no-encoding",
                "--no-empty",
                "--no-filename",
                "--no-files-with-matches",
                "--no-filter",
                "--no-glob-ignore-case",
                "--no-group-separator",
                "--no-heading",
                "--no-hidden",
                "--no-hyperlink",
                "--no-ignore-binary",
                "--no-ignore-case",
                "--no-ignore-files",
                "--no-index",
                "--no-initial-tab",
                "--no-invert-match",
                "--no-line-number",
                "--no-messages",
                "--no-mmap",
                "--no-only-line-number",
                "--no-only-matching",
                "--no-pager",
                "--no-pretty",
                "--no-smart-case",
                "--no-sort",
                "--no-split",
                "--no-stats",
                "--no-tree",
                "--no-ungroup",
                "--no-view",
            ),
            bool,
        ),
    ),
)

RG = CommandSchema(
    "rg",
    operands=(Operand("operands", count="*"),),
    options=(
        Option("patterns", ("-e", "--regexp")),
        Option("pattern_files", ("-f", "--file")),
        Option("glob", ("-g", "--glob", "--iglob")),
        Option("type", ("-t", "--type")),
        Option("type_add", ("--type-add",)),
        Option("listing", ("--files", "--type-list"), bool),
        Option("generate", ("--generate",)),
        Option("info", ("-h", "--help", "-V", "--version", "--pcre2-version"), bool),
        Option(
            "parameter",
            (
                "-A",
                "-B",
                "-C",
                "-E",
                "-M",
                "-T",
                "-d",
                "-j",
                "-m",
                "-r",
                "--after-context",
                "--before-context",
                "--color",
                "--colors",
                "--context",
                "--context-separator",
                "--dfa-size-limit",
                "--encoding",
                "--engine",
                "--field-context-separator",
                "--field-match-separator",
                "--hostname-bin",
                "--hyperlink-format",
                "--ignore-file",
                "--max-columns",
                "--max-count",
                "--max-depth",
                "--maxdepth",
                "--max-filesize",
                "--path-separator",
                "--pre",
                "--pre-glob",
                "--regex-size-limit",
                "--replace",
                "--sort",
                "--sortr",
                "--threads",
                "--type-clear",
                "--type-not",
            ),
        ),
        Option(
            "mode",
            (
                *(f"-{letter}" for letter in "0FHILNPSUabcilnopqsuvwxz."),
                "--auto-hybrid-regex",
                "--binary",
                "--block-buffered",
                "--byte-offset",
                "--case-sensitive",
                "--column",
                "--count",
                "--count-matches",
                "--crlf",
                "--debug",
                "--files-with-matches",
                "--files-without-match",
                "--fixed-strings",
                "--follow",
                "--glob-case-insensitive",
                "--heading",
                "--hidden",
                "--ignore",
                "--ignore-case",
                "--ignore-dot",
                "--ignore-exclude",
                "--ignore-file-case-insensitive",
                "--ignore-files",
                "--ignore-global",
                "--ignore-messages",
                "--ignore-parent",
                "--ignore-vcs",
                "--include-zero",
                "--invert-match",
                "--json",
                "--line-buffered",
                "--line-number",
                "--line-regexp",
                "--max-columns-preview",
                "--messages",
                "--mmap",
                "--multiline",
                "--multiline-dotall",
                "--null",
                "--null-data",
                "--one-file-system",
                "--only-matching",
                "--passthru",
                "--passthrough",
                "--pcre2",
                "--pcre2-unicode",
                "--pretty",
                "--quiet",
                "--require-git",
                "--search-zip",
                "--smart-case",
                "--sort-files",
                "--stats",
                "--stop-on-nonmatch",
                "--text",
                "--trace",
                "--trim",
                "--unicode",
                "--unrestricted",
                "--vimgrep",
                "--with-filename",
                "--word-regexp",
                "--no-auto-hybrid-regex",
                "--no-binary",
                "--no-block-buffered",
                "--no-byte-offset",
                "--no-column",
                "--no-config",
                "--no-context-separator",
                "--no-crlf",
                "--no-encoding",
                "--no-filename",
                "--no-fixed-strings",
                "--no-follow",
                "--no-glob-case-insensitive",
                "--no-heading",
                "--no-hidden",
                "--no-ignore",
                "--no-ignore-dot",
                "--no-ignore-exclude",
                "--no-ignore-file-case-insensitive",
                "--no-ignore-files",
                "--no-ignore-global",
                "--no-ignore-messages",
                "--no-ignore-parent",
                "--no-ignore-vcs",
                "--no-include-zero",
                "--no-invert-match",
                "--no-json",
                "--no-line-buffered",
                "--no-line-number",
                "--no-max-columns-preview",
                "--no-messages",
                "--no-mmap",
                "--no-multiline",
                "--no-multiline-dotall",
                "--no-one-file-system",
                "--no-pcre2",
                "--no-pcre2-unicode",
                "--no-pre",
                "--no-require-git",
                "--no-search-zip",
                "--no-sort-files",
                "--no-stats",
                "--no-text",
                "--no-trim",
                "--no-unicode",
            ),
            bool,
        ),
    ),
)


def narrows(glob: Scalar | None) -> bool:
    return isinstance(glob, str) and any(character.isalnum() for character in glob)


def names_files(target: Target) -> bool:
    match target.value:
        case str(value) if target.has_glob:
            path = Path(value)
            return narrows("".join(path.parts[len(glob_prefix(path).parts) :]))
        case _:
            return False


def broad(target: Target) -> bool:
    return not names_files(target) and (
        BROAD_ROOTS(target) or ((path := target.path) is not None and is_repo_root(glob_prefix(path)))
    )


def named_roots(arguments: Arguments) -> Targets:
    pattern_given = any(arguments.values.get(role) for role in PATTERN_ROLES)
    return Targets(
        arguments.call.resolved_targets(arguments.words.get("operands", ())[0 if pattern_given else 1 :]).targets,
        complete=arguments.operands_complete and "from" not in arguments.values,
    )


def searches_broadly(arguments: Arguments, roots: Targets) -> bool:
    return not roots.complete or any(map(broad, roots or (Target(".", ".", arguments.cwd),)))


def reads_stdin(arguments: Arguments) -> bool:
    return "-" not in arguments.values.get("pattern_files", ()) and (
        arguments.call.occurrence.prev_op == "|"
        or any(
            redirect.op in STDIN_REDIRECTS and redirect.fd in (None, 0) and redirect.target != "/dev/null"
            for redirect in arguments.call.redirects
        )
    )


def recursive(arguments: Arguments) -> bool:
    return any(arguments.values.get(role) for role in RECURSIVE_ROLES) or any(
        not isinstance(action, str) or action.casefold() in RECURSE_ACTIONS
        for action in arguments.values.get("directories", ())
    )


def grep_searches_broadly(arguments: Arguments) -> bool:
    return recursive(arguments) and searches_broadly(arguments, named_roots(arguments))


def ugrep_searches_broadly(arguments: Arguments) -> bool:
    roots = named_roots(arguments)
    return (
        recursive(arguments)
        or any(isinstance(glob, str) and "/" in glob for glob in arguments.values.get("glob", ()))
        or (roots.complete and not roots and not reads_stdin(arguments))
    ) and searches_broadly(arguments, roots)


def rg_searches_broadly(arguments: Arguments) -> bool:
    roots = named_roots(arguments)
    return not (roots.complete and not roots and reads_stdin(arguments)) and searches_broadly(arguments, roots)


def bounded(includes: tuple[Scalar | None, ...]) -> bool:
    return bool(includes) and all(map(narrows, includes))


def positive(globs: Iterable[Scalar | None], negations: tuple[str, ...]) -> tuple[Scalar | None, ...]:
    return tuple(glob for glob in globs if not (isinstance(glob, str) and glob.startswith(negations)))


def listed(values: Iterable[Scalar | None]) -> tuple[str | None, ...]:
    return tuple(piece for value in values for piece in (value.split(",") if isinstance(value, str) else (None,)))


def grep_bounded(arguments: Arguments) -> bool:
    return bounded((*arguments.values.get("include", ()), *arguments.values.get("include_dir", ())))


def ugrep_bounded(arguments: Arguments) -> bool:
    return bounded(
        (
            *positive(
                listed(
                    (
                        *arguments.values.get("glob", ()),
                        *arguments.values.get("extension", ()),
                        *arguments.values.get("type", ()),
                    )
                ),
                ("!", "^"),
            ),
            *arguments.values.get("include_dir", ()),
            *(None for _ in (*arguments.values.get("magic", ()), *arguments.values.get("include_from", ()))),
        )
    )


def rg_bounded(arguments: Arguments) -> bool:
    typed = "type_add" not in arguments.values
    return bounded(
        (
            *positive(arguments.values.get("glob", ()), ("!",)),
            *(kind if typed and kind != "all" else None for kind in arguments.values.get("type", ())),
        )
    )


def informational(arguments: Arguments) -> bool:
    return any(arguments.values.get(role) for role in INFO_ROLES)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(
            CommandMatches(GREP, only_if=(grep_searches_broadly,), skip_if=(grep_bounded, informational)),
            CommandMatches(UGREP, only_if=(ugrep_searches_broadly,), skip_if=(ugrep_bounded, informational)),
            CommandMatches(RG, only_if=(rg_searches_broadly,), skip_if=(rg_bounded, informational)),
        ),
    ],
    message=(
        "A recursive `grep`, `ugrep`, or `rg` at /, a home, a repository root, a worktree pool, or a scratch "
        "project reads every file under it. Search a deeper directory or named files, narrow it with "
        "`--include='*.py'` or `-g '*.py'`, or run `ccx code grep <text> <dir>`."
    ),
    block=True,
    tests={
        Input(command="ugrep -rn needle /Users/yasyf/.claude/scratch/release-v3"): Block(
            pattern="reads every file under it"
        ),
        Input(command="ugrep -rl needle ~/.claude/scratch/release-v3"): Block(),
        Input(command="grep -r -e needle /"): Block(),
        Input(command="grep -r -f patterns.txt ~"): Block(),
        Input(command="grep -r --regexp=needle /"): Block(),
        Input(command="sudo /usr/bin/grep -R needle /Users/alice"): Block(),
        Input(command="env X=1 timeout 10 ugrep -rn needle /Users/alice"): Block(),
        Input(command="sh -c 'cd / && ugrep -rn needle .'"): Block(),
        Input(command="grep -rn needle /Users/alice | head -1"): Block(),
        Input(command="grep -Rni needle ~"): Block(),
        Input(command="grep -d recurse needle /"): Block(),
        Input(command="grep -rn -- needle /tmp/scratch-lane"): Block(),
        Input(command="rg needle /"): Block(),
        Input(command="rg --max-count 1 needle /"): Block(),
        Input(command="rg -m1 needle ~ | head -1"): Block(),
        Input(command="rg -d 1 needle ~"): Block(),
        Input(command="rg -g'*.py' -g'**' needle ~"): Block(),
        Input(command="rg -g'!*.md' needle ~"): Block(),
        Input(command="rg -tall needle ~"): Block(),
        Input(command="rg --type-add 'mine:*' -tmine needle ~"): Block(),
        Input(command="rg -e needle -e other ~"): Block(),
        Input(command="rg needle ~/.claude/worktrees/captain-hook"): Block(),
        Input(command="ugrep -rn needle ~/.orca/workspaces/captain-hook"): Block(),
        Input(command="rg needle src; rg needle /", cwd="/repo"): Block(),
        Input(command='rg needle "$ROOT"'): Block(),
        Input(command="rg needle"): Block(),
        Input(command="grep -r --include='*' needle ~"): Block(),
        Input(command="grep -r --exclude-dir=node_modules needle ~"): Block(),
        Input(command="ugrep -2 needle /"): Block(),
        Input(command="ugrep -r -g'*.py,*' needle ~"): Block(),
        Input(command="ugrep -r -M python needle ~"): Block(),
        Input(command="ugrep -r --and needle --not other ~"): Block(),
        Input(command="grep -n needle explicit.log", cwd="/repo"): Allow(),
        Input(command="rg needle config.toml tests/test_specific.py", cwd="/repo"): Allow(),
        Input(command="ugrep -rn needle ~/.claude/scratch/release-v3/specific-lane"): Allow(),
        Input(command="grep -rn --include='*.py' needle ~"): Allow(),
        Input(command="grep -rn --include='*.py' --include='*.json' needle ~"): Allow(),
        Input(command="rg -g'*.json' needle ~"): Allow(),
        Input(command="rg -tpy needle ~"): Allow(),
        Input(command="ugrep -r -tpy,json needle ~"): Allow(),
        Input(command="ugrep -g 'src/*.py' needle ~"): Allow(),
        Input(command="grep -rn needle *.py", cwd="/"): Allow(),
        Input(command="cat explicit.log | rg needle"): Allow(),
        Input(command="rg needle <explicit.log"): Allow(),
        Input(command="cat explicit.log | ugrep needle"): Allow(),
        Input(command="rg --files ~"): Allow(),
        Input(command="rg --type-list"): Allow(),
        Input(command="rg needle / --help"): Allow(),
        Input(command="echo 'ugrep -rn needle /'"): Allow(),
        Input(command="rg '/' src", cwd="/repo"): Allow(),
        Input(command="rg -e / src", cwd="/repo"): Allow(),
        Input(command="grep -rn -- '~' src", cwd="/repo"): Allow(),
        Input(command="ugrep needle /"): Allow(),
        Input(command="git grep needle"): Allow(),
    },
)
