from __future__ import annotations

import os
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    CommandMatches,
    CommandSchema,
    CustomCondition,
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
from captain_hook.bindings import dequote
from captain_hook.app import current_state
from captain_hook.cmd import git_location
from captain_hook.command_schema import glob_prefix
from captain_hook.util.scratch import TEMP_ROOTS
from captain_hook.util.vcs import common_git_dir, git_dir, is_repo_root

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from cc_transcript.command import Word

    from captain_hook import Arguments, BaseHookEvent
    from captain_hook.cmd import Call
    from captain_hook.command_schema import Scalar

POOLS = (
    ".claude/worktrees",
    ".orca/workspaces",
    "repos/claude/worktrees",
    ".daemonkit/a/ccx-cleanup/jobs",
)
HOME_POOL_LEVELS = dict.fromkeys(POOLS, 1) | {"repos/orca/workspaces": 2}
POOL_ROOTS = PathMatches(tuple(f"**/{pool}" for pool in HOME_POOL_LEVELS), unresolved=True)
POOL_MEMBERS = PathMatches(
    tuple(
        f"**/{pool}{'/*' * level}"
        for pool, levels in HOME_POOL_LEVELS.items()
        for level in range(1, levels + 1)
    )
)
BROAD_ROOTS = PathMatches(
    (
        "/",
        "~",
        "/Users",
        "/Users/*",
        "/home",
        "/home/*",
        "/System/Volumes/Data",
        *POOL_ROOTS.patterns,
        *POOL_MEMBERS.patterns,
        "**/.claude/scratch",
        "**/.claude/scratch/*",
        *(pattern for root in TEMP_ROOTS for pattern in (str(root), f"{root}/*")),
    ),
    unresolved=True,
)
PATTERN_ROLES = ("patterns", "pattern_files", "query")
RECURSIVE_ROLES = ("recursive", "depth", "include_dir")
INFO_ROLES = ("info", "listing", "generate")
RG_OUTPUT_ROLES = ("files", "listing", "generate")
RECURSE_ACTIONS = frozenset({"rec", "recu", "recur", "recurs", "recurse", "dereference-recurse"})
STDIN_REDIRECTS = frozenset({"<", "<<<"})
GIT_GREP_GROUPS = frozenset({"(", ")"})
SHORT_MAGIC = frozenset({"/", "!", "^"})
GIT_LOCATION_OPTIONS = frozenset({"--git-dir", "--work-tree"})
GIT_LOCATION_VARIABLES = frozenset({"GIT_DIR", "GIT_WORK_TREE"})

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
        Option("query", ("-Q", "--query", "--match"), bool),
        Option(
            "recursive",
            ("-r", "-R", "--recursive", "--dereference-recursive", "--index", *(f"-{depth}" for depth in range(1, 10))),
            bool,
        ),
        Option("depth", ("--depth", "--max-depth", "--min-depth")),
        Option("directories", ("-d", "--directories")),
        Option("glob", ("-g", "--glob", "--iglob", "--include")),
        Option("include_dir", ("--include-dir",)),
        Option("include_from", ("--include-from",)),
        Option("extension", ("-O", "--file-extension")),
        Option("type", ("-t", "--file-type")),
        Option("magic", ("-M", "--file-magic")),
        Option("exclude", ("--exclude", "--exclude-dir", "--exclude-from")),
        Option("from", ("--from",)),
        Option("all", ("--all", "-@"), bool),
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
                *(f"-{letter}" for letter in "0EFGHILPSTUWXYZabchijklnopqsuvwxyz%.+^"),
                "--text",
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
                "--exclude-fs",
                "--include-fs",
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
        Option("files", ("--files",), bool),
        Option("listing", ("--type-list",), bool),
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


GIT_GREP = CommandSchema(
    "git",
    subcommand=("grep",),
    clustered_values=True,
    operands=(Operand("operands", count="*"),),
    options=(
        Option("patterns", ("-e",)),
        Option("pattern_files", ("-f",)),
        Option("info", ("--help",), bool),
        Option(
            "parameter",
            (
                "-A",
                "-B",
                "-C",
                "-m",
                "--after-context",
                "--before-context",
                "--context",
                "--max-count",
                "--max-depth",
                "--threads",
            ),
        ),
        Option(
            "mode",
            (
                *(f"-{digit}" for digit in range(10)),
                *(f"-{letter}" for letter in "EFGHILPWachilnopqrvwz"),
                "--cached",
                "--no-cached",
                "--index",
                "--no-index",
                "--untracked",
                "--no-untracked",
                "--exclude-standard",
                "--no-exclude-standard",
                "--recurse-submodules",
                "--no-recurse-submodules",
                "--invert-match",
                "--no-invert-match",
                "--ignore-case",
                "--no-ignore-case",
                "--word-regexp",
                "--no-word-regexp",
                "--text",
                "--no-text",
                "--textconv",
                "--no-textconv",
                "--recursive",
                "--no-recursive",
                "--extended-regexp",
                "--no-extended-regexp",
                "--basic-regexp",
                "--no-basic-regexp",
                "--fixed-strings",
                "--no-fixed-strings",
                "--perl-regexp",
                "--no-perl-regexp",
                "--line-number",
                "--no-line-number",
                "--column",
                "--no-column",
                "--full-name",
                "--no-full-name",
                "--files-with-matches",
                "--no-files-with-matches",
                "--name-only",
                "--no-name-only",
                "--files-without-match",
                "--no-files-without-match",
                "--null",
                "--no-null",
                "--only-matching",
                "--no-only-matching",
                "--count",
                "--no-count",
                "--color",
                "--no-color",
                "--break",
                "--no-break",
                "--heading",
                "--no-heading",
                "--no-context",
                "--no-threads",
                "--show-function",
                "--no-show-function",
                "--function-context",
                "--no-function-context",
                "--and",
                "--or",
                "--not",
                "--quiet",
                "--no-quiet",
                "--all-match",
                "--no-all-match",
                "--open-files-in-pager",
                "--no-open-files-in-pager",
                "--ext-grep",
                "--no-ext-grep",
                "--no-max-count",
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


def written(target: Target) -> Path | None:
    if target.value is None:
        return None
    path = Path(os.path.expanduser(target.value) if target.raw.startswith("~") else target.value)
    root = path if path.is_absolute() else None if target.cwd is None else target.cwd / path
    return None if root is None else glob_prefix(root)


def canonical(target: Target) -> Path | None:
    return None if (root := written(target)) is None else root.resolve()


def at_home(target: Target) -> bool:
    return canonical(target) == Path.home().resolve()


def pool_anchors() -> dict[Path, int]:
    return {Path.home() / pool: levels for pool, levels in HOME_POOL_LEVELS.items()}


def encloses_roots(root: Path, anchors: Iterable[Path]) -> bool:
    return root in {
        ancestor.resolve()
        for anchor in anchors
        for ancestor in (anchor, *anchor.parents, *anchor.resolve().parents)
    }


def encloses_home_or_pool(root: Path) -> bool:
    return encloses_roots(root, (Path.home(), *pool_anchors()))


def known_search_roots(cwd: Path | None) -> tuple[Path, ...]:
    checkout = (
        next((ancestor for ancestor in (cwd, *cwd.parents) if is_repo_root(ancestor)), None)
        if cwd is not None and cwd.is_absolute()
        else None
    )
    git = git_dir(checkout) if checkout is not None else None
    common = common_git_dir(git) if git is not None else None
    packs = (
        Path(registered.state_root)
        for registered in current_state().hooks
        if registered.pack_name is not None
        and registered.state_root is not None
        and Path(registered.state_root).is_absolute()
    )
    return tuple(dict.fromkeys((
        *((checkout,) if checkout is not None else ()),
        *((common.parent,) if common is not None and common.name == ".git" and common.is_dir() else ()),
        *packs,
    )))


def pool_member(target: Target, lexical: Path, root: Path) -> bool:
    return POOL_MEMBERS(target) or any(
        anchor in path.parents[:levels]
        for pool, levels in pool_anchors().items()
        for anchor in (pool, pool.resolve())
        for path in (Path(os.path.normpath(lexical)), root)
    )


def spans_checkouts(target: Target) -> bool:
    if (path := target.path) is not None and path.is_file():
        return False
    if (lexical := written(target)) is None:
        return True
    root = lexical.resolve()
    return (
        POOL_ROOTS(target)
        or encloses_home_or_pool(root)
        or (pool_member(target, lexical, root) and not is_repo_root(root))
    )


def broad(target: Target, *, known_roots: tuple[Path, ...] | None = None) -> bool:
    path = target.path
    return (
        not names_files(target)
        and not (path is not None and path.is_file())
        and (
            BROAD_ROOTS(target)
            or at_home(target)
            or (path is not None and is_repo_root(glob_prefix(path)))
            or (
                (root := canonical(target)) is not None
                and encloses_roots(root, known_search_roots(target.cwd) if known_roots is None else known_roots)
            )
        )
    )


def named_roots(arguments: Arguments) -> Targets:
    pattern_given = any(arguments.values.get(role) for role in PATTERN_ROLES)
    return Targets(
        arguments.call.resolved_targets(arguments.words.get("operands", ())[0 if pattern_given else 1 :]).targets,
        complete=arguments.operands_complete and "from" not in arguments.values,
    )


def searches_broadly(
    roots: Targets, cwd: Path | None, *, known_roots: tuple[Path, ...] | None = None
) -> bool:
    known = known_search_roots(cwd) if known_roots is None else known_roots
    return not roots.complete or any(broad(target, known_roots=known) for target in roots or (Target(".", ".", cwd),))


class BroadMCPGrep(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        fields = evt.input.raw
        repo = fields.get("repo", "")
        paths = fields.get("paths", [])
        globs = fields.get("globs", [])
        if not (
            isinstance(repo, str)
            and isinstance(paths, list)
            and all(isinstance(path, str) for path in paths)
            and isinstance(globs, list)
            and all(isinstance(glob, str) for glob in globs)
        ):
            return True
        if bounded(positive(tuple(globs), ("!",))):
            return False
        root = Path(os.path.normpath(repo)) if os.path.isabs(repo) else None
        resolved = tuple(path for path in paths if os.path.isabs(path)) + (
            tuple(
                os.path.normpath(root / path)
                for path in paths
                if path and not path.startswith("~") and not os.path.isabs(path)
            )
            if root is not None
            else ()
        )
        targets = tuple(Target(path, path, None) for path in resolved)
        if not targets and (paths or root is None):
            return False
        roots = Targets(targets)
        return searches_broadly(roots, root, known_roots=known_search_roots(evt.cwd)) or any(map(spans_checkouts, roots))


def reads_stdin(arguments: Arguments) -> bool:
    return "-" not in arguments.values.get("pattern_files", ()) and (
        source != "/dev/null"
        if (
            source := next(
                (
                    redirect.target
                    for redirect in reversed(arguments.call.redirects)
                    if redirect.op in STDIN_REDIRECTS and redirect.fd in (None, 0)
                ),
                None,
            )
        )
        is not None
        else arguments.call.occurrence.prev_op == "|"
    )


def recursive(arguments: Arguments) -> bool:
    return any(arguments.values.get(role) for role in RECURSIVE_ROLES) or any(
        not isinstance(action, str) or action.casefold() in RECURSE_ACTIONS
        for action in arguments.values.get("directories", ())
    )


def grep_searches_broadly(arguments: Arguments) -> bool:
    return recursive(arguments) and (
        not (roots := named_roots(arguments)).complete
        or any(
            target.value != "-" and (target.has_glob or not ((path := target.path) is not None and path.is_file()))
            for target in roots or (Target(".", ".", arguments.cwd),)
        )
    )


def ugrep_searches_broadly(arguments: Arguments) -> bool:
    roots = named_roots(arguments)
    return (
        recursive(arguments)
        or any(isinstance(glob, str) and "/" in glob for glob in arguments.values.get("glob", ()))
        or (roots.complete and not roots and not reads_stdin(arguments))
    ) and searches_broadly(roots, arguments.cwd, known_roots=known_search_roots(arguments.call.cmd.cwd))


def rg_searches_broadly(arguments: Arguments) -> bool:
    roots = named_roots(arguments)
    return not (roots.complete and not roots and reads_stdin(arguments)) and searches_broadly(
        roots, arguments.cwd, known_roots=known_search_roots(arguments.call.cmd.cwd)
    )


def pathspec_magic(value: str) -> tuple[frozenset[str], str]:
    if value.startswith(":("):
        close = value.find(")")
        return (
            (frozenset({"unparsed"}), value)
            if close < 0
            else (frozenset(part.split(":", 1)[0] for part in value[2:close].split(",")), value[close + 1 :])
        )
    rest = value[1:]
    short = ""
    while rest[:1] in SHORT_MAGIC:
        short, rest = short + rest[0], rest[1:]
    return (
        frozenset({*(("top",) if "/" in short else ()), *(("exclude",) if {"!", "^"} & set(short) else ())}),
        rest.removeprefix(":"),
    )


def top_broad(target: Target) -> bool:
    return (
        target.value is None or os.path.isabs(os.path.expanduser(target.value)) or not narrows(target.value.strip("/"))
    )


def pathspec_broad(
    target: Target, *, relocated: bool, known_roots: tuple[Path, ...] | None = None
) -> bool | None:
    if target.value is None:
        return broad(target, known_roots=known_roots)
    magic = target.value.startswith(":")
    kinds, pattern = pathspec_magic(target.value) if magic else (frozenset(), target.value)
    if "exclude" in kinds:
        return None
    if "top" in kinds:
        return top_broad(Target(pattern or ".", pattern or ".", target.cwd))
    spec = Target(os.path.normpath(pattern), pattern if magic else target.raw, target.cwd)
    return top_broad(spec) if relocated else broad(spec, known_roots=known_roots)


def host_calls(call: Call) -> Iterator[Call]:
    while (host := call.occurrence.host) is not None:
        call = next(candidate for candidate in call.cmd.calls() if candidate.occurrence.index == host.index)
        yield call


def assigned_names(call: Call) -> set[str]:
    wrapped = call.source.words[: len(call.source.words) - len(call.command.words)]
    return {name for name, _ in call.source.env} | {dequote(word.raw).partition("=")[0] for word in wrapped}


def git_relocated(call: Call) -> bool:
    scope, _ = call.cmd.scope_for(call, call.command.words[0])
    environment = (
        assigned_names(call) | {name for host in host_calls(call) for name in assigned_names(host)} | scope.exported
    )
    return not (
        GIT_LOCATION_OPTIONS.isdisjoint(option.partition("=")[0] for option in call.leading_options)
        and GIT_LOCATION_VARIABLES.isdisjoint(environment)
    )


def git_cwd(call: Call) -> Path | None:
    leading = call.command.words[: 1 + len(call.leading_options)]
    return (
        None
        if any(
            word.value is None and (flag.value == "-C" or word.raw.startswith("-C")) for flag, word in pairwise(leading)
        )
        else git_location(call, None)[0]
    )


def names_path(target: Target) -> bool:
    return (target.value or "").startswith(":") or target.has_glob or (path := target.path) is None or path.exists()


def git_grep_operands(arguments: Arguments) -> tuple[list[Word], list[Word] | None] | None:
    operands = arguments.words.get("operands", ())
    head = [word for word in operands[: arguments.dashdash] if word.value not in GIT_GREP_GROUPS]
    tail = None if arguments.dashdash is None else list(operands[arguments.dashdash :])
    if any(arguments.values.get(role) for role in PATTERN_ROLES):
        return head, tail
    if head:
        return head[1:], tail
    if not tail:
        return None
    rest = tail[1:]
    split = next((index for index, word in enumerate(rest) if word.value == "--"), None)
    return (rest, None) if split is None else (rest[:split], rest[split + 1 :])


def git_grep_searches_broadly(arguments: Arguments) -> bool:
    if not arguments.operands_complete:
        return True
    if (operands := git_grep_operands(arguments)) is None:
        return False
    call, cwd, relocated = arguments.call, git_cwd(arguments.call), git_relocated(arguments.call)
    head, tail = operands
    targets = [
        Target(target.value, target.raw, cwd)
        for word in (head if tail is None else tail)
        for target in call.word_targets(word)
    ]
    pathspecs = [target for target in targets if names_path(target)] if tail is None else targets
    known = known_search_roots(call.cmd.cwd)
    verdicts = [
        verdict
        for target in pathspecs
        if (verdict := pathspec_broad(target, relocated=relocated, known_roots=known)) is not None
    ]
    return any(verdicts or (relocated or broad(Target(".", ".", cwd), known_roots=known),))


def bounded(includes: tuple[Scalar | None, ...]) -> bool:
    return bool(includes) and all(map(narrows, includes))


def positive(globs: Iterable[Scalar | None], negations: tuple[str, ...]) -> tuple[Scalar | None, ...]:
    return tuple(glob for glob in globs if not (isinstance(glob, str) and glob.startswith(negations)))


def listed(values: Iterable[Scalar | None]) -> tuple[str | None, ...]:
    return tuple(piece for value in values for piece in (value.split(",") if isinstance(value, str) else (None,)))


def grep_bounded(arguments: Arguments) -> bool:
    return bounded((*arguments.values.get("include", ()), *arguments.values.get("include_dir", ())))


def ugrep_bounded(arguments: Arguments) -> bool:
    return not arguments.values.get("all") and bounded(
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


def lists_files(arguments: Arguments) -> bool:
    outputs = [role for role in arguments.order if role in RG_OUTPUT_ROLES]
    return not arguments.values.get("info") and (
        outputs[-1:] == ["files"] or (not arguments.complete and "files" in outputs)
    )


def lists_home_or_pool(arguments: Arguments) -> bool:
    roots = arguments.paths("operands")
    return not roots.complete or any(map(spans_checkouts, roots or (Target(".", ".", arguments.cwd),)))


hook(
    Event.PreToolUse,
    only_if=[Tool("ccx_code_grep"), BroadMCPGrep()],
    message=(
        "A structured `ccx_code_grep` request over a home, repository root, worktree pool, or an ancestor of "
        "a known checkout or loaded pack is a broad search. "
        "Name files or a focused directory in `paths`, or add a filename bound such as `globs: ['*.py']`. "
        "The output `budget` does not bound the files searched."
    ),
    block=True,
    max_fires=None,
    tests={
        Input(tool="mcp__cc_context__ccx_code_grep", tool_input={"text": "needle", "paths": ["/"]}): Block(),
        Input(
            tool="mcp__plugin_cc-context_cc-context__ccx_code_grep",
            tool_input={"text": "needle", "repo": "/"},
        ): Block(),
        Input(
            tool="mcp__cc_context__ccx_code_grep",
            tool_input={"text": "needle", "repo": "/repo", "paths": ["src/specific.py"]},
        ): Allow(),
        Input(
            tool="mcp__cc_context__ccx_code_grep", tool_input={"text": "needle", "globs": ["*.py"]}, cwd="/repo"
        ): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(
            CommandMatches(GREP, only_if=(grep_searches_broadly,), skip_if=(grep_bounded, informational)),
            CommandMatches(UGREP, only_if=(ugrep_searches_broadly,), skip_if=(ugrep_bounded, informational)),
            CommandMatches(RG, only_if=(rg_searches_broadly,), skip_if=(rg_bounded, informational, lists_files)),
            CommandMatches(GIT_GREP, only_if=(git_grep_searches_broadly,), skip_if=(informational,)),
        ),
    ],
    message=(
        "A recursive `grep` over a directory reads every file under it; bound `grep` by filename and scope other "
        "searches below broad roots. Name files, use `--include='*.py'`, `-g '*.py'`, or `-- '*.py'`, or run "
        "`ccx code grep` with scoped paths."
    ),
    block=True,
    max_fires=None,
    tests={
        Input(command="ugrep -rn needle /Users/yasyf/.claude/scratch/release-v3"): Block(
            pattern="reads every file under it"
        ),
        Input(command="ugrep -rl needle ~/.claude/scratch/release-v3"): Block(),
        Input(
            command='grep -rl "backup in cloud storage" ~/.claude/scratch/durable-agents-rebuild '
            "2>/dev/null | grep -v node_modules | head -3"
        ): Block(),
        Input(command="grep -r -e needle /"): Block(),
        Input(command="grep -r -f patterns.txt ~"): Block(),
        Input(command="grep -r --regexp=needle /"): Block(),
        Input(command="sudo /usr/bin/grep -R needle /Users/alice"): Block(),
        Input(command="env X=1 timeout 10 ugrep -rn needle /Users/alice"): Block(),
        Input(command="sh -c 'cd / && ugrep -rn needle .'"): Block(),
        Input(command="grep -rn needle /Users/alice | head -1"): Block(),
        Input(command=r'grep -rn "CCI_RELAY_URL\s*=" infra/', cwd="/repo"): Block(),
        Input(command="grep -rIn needle infra/", cwd="/repo"): Block(),
        Input(command="grep -rn --include='*' needle infra/", cwd="/repo"): Block(),
        Input(command="grep -r -e needle internal", cwd="/repo"): Block(),
        Input(command="grep -rn needle internal/f*", cwd="/repo"): Block(),
        Input(command="grep -rn needle - infra/", cwd="/repo"): Block(),
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
        Input(command="rg needle ~/repos/claude/worktrees"): Block(),
        Input(command="rg needle ~/repos/claude/worktrees/captain-hook"): Block(),
        Input(command="rg needle ~/repos/orca/workspaces"): Block(),
        Input(command="rg needle ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17"): Block(),
        Input(
            command="rg needle ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17/captain-hook"
        ): Block(),
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
        Input(command="ugrep -r -g'*.py' --all needle ~"): Block(),
        Input(command="ugrep --include-fs -r needle ~"): Block(),
        Input(command="ugrep --max-depth=3 needle /"): Block(),
        Input(command="ugrep -r --match /"): Block(),
        Input(command="printf needle | rg needle </dev/null"): Block(),
        Input(command="git grep needle"): Block(),
        Input(command="git -C / grep needle"): Block(),
        Input(command="git -C ~ grep -n needle"): Block(),
        Input(command="git -C ~/.claude/worktrees/captain-hook grep needle"): Block(),
        Input(command='git -C "$WORKTREE" grep needle'): Block(),
        Input(command="git -c core.quotepath=off -C ~ grep -c needle"): Block(),
        Input(command="sh -c 'cd ~ && git grep needle'"): Block(),
        Input(command="git grep -n -e needle -- .", cwd="/Users/alice"): Block(),
        Input(command="git grep needle -- ':!vendor'", cwd="/Users/alice"): Block(),
        Input(command="git grep needle HEAD -- .", cwd="/Users/alice"): Block(),
        Input(command="git grep needle HEAD", cwd="/Users/alice"): Block(),
        Input(command="git grep --bogus-flag needle -- src", cwd="/Users/alice"): Block(),
        Input(command="git grep needle -- :/"): Block(),
        Input(command="git grep -- needle", cwd="/Users/alice"): Block(),
        Input(command="SPEC=':/'; git grep needle -- \"$SPEC\""): Block(),
        Input(command="git grep needle -- ':(top,glob)**/**'"): Block(),
        Input(command="git --namespace -Cinternal grep needle", cwd="/Users/alice"): Block(),
        Input(command="git --git-dir=/srv/demo/.git grep --cached needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="git --git-dir /srv/demo/.git grep needle -- .", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="GIT_DIR=/srv/demo/.git git grep --cached needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="env GIT_DIR=/srv/demo/.git git grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="export GIT_DIR=/srv/demo/.git; git grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="git --work-tree=/srv/demo grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(
            command="git --git-dir=/srv/demo/.git grep --cached needle -- internal/..",
            cwd="/srv/demo/internal/focused",
        ): Block(),
        Input(command="git grep needle -- src/..", cwd="/Users/alice"): Block(),
        Input(command="env 'GIT_DIR=/srv/demo/.git' git grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="GIT_DIR=/srv/demo/.git sh -c 'git grep needle'", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="git grep -nC3 needle", cwd="/Users/alice"): Block(),
        Input(command="git grep -nC 3 needle -- .", cwd="/Users/alice"): Block(),
        Input(command="git grep -nOvim needle -- src", cwd="/Users/alice"): Block(),
        Input(command="grep -n needle explicit.log", cwd="/repo"): Allow(),
        Input(command="grep -rn needle -", cwd="/repo"): Allow(),
        Input(command="rg needle config.toml tests/test_specific.py", cwd="/repo"): Allow(),
        Input(command="ugrep -rn needle ~/.claude/scratch/release-v3/specific-lane"): Allow(),
        Input(command="grep -rn --include='*.py' needle ~"): Allow(),
        Input(command="grep -rIn --include='*.py' needle infra/", cwd="/repo"): Allow(),
        Input(command="grep -rn --include='*.py' --include='*.json' needle ~"): Allow(),
        Input(command="rg -g'*.json' needle ~"): Allow(),
        Input(command="rg -tpy needle ~"): Allow(),
        Input(command="ugrep -r -tpy,json needle ~"): Allow(),
        Input(command="ugrep -g 'src/*.py' needle ~"): Allow(),
        Input(command="grep -rn needle *.py", cwd="/"): Block(),
        Input(command="grep -rn --include='*.py' needle internal/f*", cwd="/repo"): Allow(),
        Input(command="cat explicit.log | rg needle"): Allow(),
        Input(command="rg needle <explicit.log"): Allow(),
        Input(command="cat explicit.log | ugrep needle"): Allow(),
        Input(command="rg --files /tmp/scratch-lane"): Allow(),
        Input(command="rg --type-list"): Allow(),
        Input(command="rg needle / --help"): Allow(),
        Input(command="echo 'ugrep -rn needle /'"): Allow(),
        Input(command="rg '/' src", cwd="/repo"): Allow(),
        Input(command="rg -e / src", cwd="/repo"): Allow(),
        Input(command="grep -rn -- '~' -", cwd="/repo"): Allow(),
        Input(command="grep -rn -- '~' src", cwd="/repo"): Block(),
        Input(command="ugrep needle /"): Allow(),
        Input(command="git grep needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git grep -c needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git -C /srv/project/internal grep needle"): Allow(),
        Input(command="git -C /srv/demo/internal/focused grep needle"): Allow(),
        Input(
            command="git --git-dir=/srv/demo/.git grep --cached needle -- internal/focused",
            cwd="/srv/demo/internal/focused",
        ): Allow(),
        Input(command="git grep -nC3 needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git grep needle -- src/../lib", cwd="/Users/alice"): Allow(),
        Input(command="git grep -nC 3 needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git grep needle -- '*.py'"): Allow(),
        Input(command="git grep -e needle -- ':(glob)**/*.py'"): Allow(),
        Input(command="git grep needle -- :/internal"): Allow(),
        Input(command="git grep --help"): Allow(),
        Input(command="git --help grep"): Allow(),
        Input(command="git grep"): Allow(),
        Input(command="echo 'git grep needle'"): Allow(),
    },
)

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandMatches(RG, only_if=(lists_files, lists_home_or_pool))],
    message=(
        "`rg --files` over `~`, a worktree pool, or any directory above them is blocked even with `-g` or `-t` "
        "filename filters. List one checkout or a deeper subtree instead: `rg --files <checkout>` or "
        "`rg --files <checkout>/<dir>`."
    ),
    block=True,
    max_fires=None,
    tests={
        Input(
            command="rg --files -g AGENTS.md -g CLAUDE.md "
            "/Users/yasyf/.claude/worktrees/captain-hook/language-shared-cache "
            "/Users/yasyf/.claude/worktrees/captain-hook /Users/yasyf/.claude/worktrees"
        ): Block(pattern="blocked even with"),
        Input(command="rg --files /Users/yasyf/.claude/worktrees/captain-hook"): Block(),
        Input(command="rg --files -g AGENTS.md /Users/yasyf/.claude/worktrees/captain-hook"): Block(),
        Input(command="rg --files /Users/yasyf/.claude/worktrees"): Block(),
        Input(command="rg --files --glob=CLAUDE.md /Users/yasyf/.claude/worktrees"): Block(),
        Input(command="rg --files -gAGENTS.md ~/.orca/workspaces/captain-hook"): Block(),
        Input(command="rg --files ~"): Block(),
        Input(command="rg --files -g '*.py' ~"): Block(),
        Input(command="rg --files --iglob=agents.md -tmd ~"): Block(),
        Input(command='rg --files "$HOME"'): Block(),
        Input(command="cd ~ && rg --files"): Block(),
        Input(command="rg --files src ~/.claude/worktrees", cwd="/repo"): Block(),
        Input(command="sh -c 'rg --files -g AGENTS.md ~/.claude/worktrees' | head -5"): Block(),
        Input(command="rg --type-list --files ~"): Block(),
        Input(command="rg --generate=man --files ~"): Block(),
        Input(command="rg --generate man --files ~"): Block(),
        Input(command="rg --files --type-list --files ~"): Block(),
        Input(command="rg --files --type-list -nC3 --files ~"): Block(),
        Input(command="rg --files ~ --type-list -nC3 --files"): Block(),
        Input(command='rg --files -g AGENTS.md "$POOL"'): Block(),
        Input(command="rg --files /"): Block(),
        Input(command="rg --files ~/.."): Block(),
        Input(command="rg --files ~/.claude"): Block(),
        Input(command="rg --files -g AGENTS.md ~/.claude"): Block(),
        Input(command="rg --files ~/.orca"): Block(),
        Input(command="cd ~/.claude && rg --files"): Block(),
        Input(command="rg --files .claude/worktrees", cwd="/repo"): Block(),
        Input(command="rg --files -g AGENTS.md .claude/worktrees/stale", cwd="/repo"): Block(),
        Input(command="rg --files ~/repos/claude/worktrees"): Block(),
        Input(command="rg --files -g AGENTS.md ~/repos/claude/worktrees/captain-hook"): Block(),
        Input(command="rg --files ~/repos/claude/worktrees/captain-hook/lane"): Allow(),
        Input(command="rg --files ~/repos/orca/workspaces"): Block(),
        Input(command="rg --files ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17"): Block(),
        Input(
            command="rg --files -g AGENTS.md ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17/captain-hook"
        ): Block(),
        Input(command="rg --files ~/repos/orca"): Block(),
        Input(command="rg --files ~/repos"): Block(),
        Input(
            command="rg --files -g AGENTS.md /Users/yasyf/.claude/worktrees/captain-hook/language-shared-cache"
        ): Allow(),
        Input(command="rg --files ~/.claude/worktrees/captain-hook/lane"): Allow(),
        Input(command="rg --files ~/.claude/worktrees/captain-hook/lane/src"): Allow(),
        Input(command="rg --files ~/.claude/scratch"): Allow(),
        Input(command="rg --files ~/.config"): Allow(),
        Input(
            command="rg --files ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17/captain-hook/lane"
        ): Allow(),
        Input(command="rg --files .", cwd="/repo"): Allow(),
        Input(command="rg --files", cwd="/repo"): Allow(),
        Input(command="rg --files -g AGENTS.md src/internal", cwd="/repo"): Allow(),
        Input(command="rg --files /tmp/scratch-lane"): Allow(),
        Input(command="rg --files --help ~"): Allow(),
        Input(command="rg --type-list"): Allow(),
        Input(command="rg --files --type-list ~"): Allow(),
        Input(command="rg --files --generate=man ~"): Allow(),
        Input(command="rg --files --generate man ~"): Allow(),
        Input(command="rg needle ~/.claude/worktrees"): Allow(),
        Input(command="echo 'rg --files ~'"): Allow(),
    },
)
