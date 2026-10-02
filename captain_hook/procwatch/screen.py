from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

SEGMENT_BREAK = re.compile(r"&&|\|\||[;|\n`]|\$\(")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WORD_BREAK = re.compile(r"[^a-z0-9]+")
VERSIONED = re.compile(r"^(python|pypy|ruby|perl|node)[0-9.]*$")
STRIPPED = "'\"()"
WRAPPERS = frozenset({"env", "sudo", "nohup", "time", "exec", "eval", "command", "xargs", "nice", "caffeinate"})
SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh"})
EXECUTORS = frozenset({"npx", "bunx", "pnpx"})
EXECUTOR_VERBS = {"pnpm": {"dlx", "exec"}, "yarn": {"dlx", "exec"}, "bun": {"x"}, "npm": {"exec"}}
RUN_VERBS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "rye": "run", "deno": "run"}
DEPLOY_WORDS = frozenset({"deploy", "release", "publish"})
SCRIPT_WORDS = DEPLOY_WORDS | frozenset(
    {"migrate", "migration", "migrations", "rollout", "provision", "terraform", "ansible"}
)
RUNNERS = frozenset({"make", "just", "task", "npm", "pnpm", "yarn", "bun", "rake", "mix", "poetry"})
PACKAGE_PUBLISHERS = frozenset({"npm", "pnpm", "yarn", "bun", "uv", "cargo", "poetry"})
CLOUD_CLIS = frozenset(
    {"fly", "flyctl", "vercel", "railway", "netlify", "heroku", "aws", "gcloud", "az", "firebase", "wrangler"}
)
CLOUD_VERBS = frozenset({"deploy", "up", "publish", "release", "--prod", "create", "update", "delete", "apply"})
DB_CLIENTS = frozenset({"psql", "mysql", "mongo", "mongosh", "redis-cli", "sqlite3"})
MIGRATORS = frozenset({"alembic", "flyway", "liquibase", "dbmate", "goose"})
KUBECTL_VERBS = frozenset({"apply", "rollout", "delete", "create", "replace", "patch", "scale", "edit"})
HELM_VERBS = frozenset({"install", "upgrade", "uninstall", "rollback", "delete"})
UNKNOWN_OPTION = "passes an option the monitor cannot classify"
INLINE_CODE = "runs inline interpreter code the monitor cannot classify"


@dataclass(frozen=True, slots=True)
class Options:
    switches: re.Pattern[str]
    valued: frozenset[str] = frozenset()
    attached: tuple[str, ...] = ()
    inline: frozenset[str] = frozenset()
    module: str | None = None


@dataclass(frozen=True, slots=True)
class Launch:
    tokens: list[str]
    module: bool = False


PYTHON = Options(
    re.compile(r"^-[bBdEiIOPqRsSuvx]+$|^-(?:h|V|VV|bb|OO)$|^--(?:help|version)$"),
    valued=frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    attached=("-W", "-X"),
    inline=frozenset({"-c"}),
    module="-m",
)
NODE = Options(
    re.compile(
        r"^-[vhc]$|^--(?:version|help|check|no-warnings|enable-source-maps|trace-warnings|trace-uncaught|expose-gc"
        r"|preserve-symlinks|preserve-symlinks-main|no-deprecation|trace-deprecation|throw-deprecation"
        r"|pending-deprecation|abort-on-uncaught-exception|frozen-intrinsics|jitless|inspect|inspect-brk"
        r"|experimental-[a-z-]+|harmony|no-experimental-[a-z-]+|watch|watch-preserve-output)$"
    ),
    valued=frozenset(
        {
            "-r",
            "--require",
            "--import",
            "--loader",
            "--experimental-loader",
            "--input-type",
            "--env-file",
            "--title",
            "--stack-size",
            "--max-old-space-size",
            "-C",
            "--conditions",
            "--inspect-port",
        }
    ),
    inline=frozenset({"-e", "--eval", "-p", "--print"}),
)
SHELL = Options(
    re.compile(r"^[-+][abefhkmnptuvxBCEHPT]+$|^--(?:noprofile|norc|posix|login|verbose|restricted|version|help)$"),
    valued=frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}),
)
RUBY = Options(
    re.compile(r"^-[wWdvanplsSyh]+$|^--(?:version|verbose|debug|disable-gems|jit|yjit|help)$"),
    valued=frozenset({"-I", "-r", "-C", "-E", "-x", "-F", "-T", "-0", "-i"}),
    attached=("-I", "-r", "-C", "-E"),
    inline=frozenset({"-e"}),
)
PERL = Options(
    re.compile(r"^-[wWcnplsTtvX]+$|^--(?:version|help)$"),
    valued=frozenset({"-I", "-M", "-m", "-F", "-i", "-l", "-x", "-0", "-C", "-D", "-d"}),
    attached=("-I", "-M", "-m", "-i", "-l", "-0", "-F"),
    inline=frozenset({"-e", "-E"}),
)
DENO = Options(
    re.compile(r"^-A$|^--(?:allow-[a-z-]+|unstable[a-z-]*|quiet|watch|no-check|no-lock|cached-only|no-remote|no-npm)$"),
    valued=frozenset(
        {"-c", "--config", "--import-map", "--lock", "--cert", "--seed", "--v8-flags", "--node-modules-dir"}
    ),
)
BUN = Options(
    re.compile(r"^--(?:watch|hot|smol|bun|silent|no-install|install)$"),
    valued=frozenset({"-r", "--preload", "--env-file", "--define", "--loader", "--tsconfig-override", "--cwd"}),
    inline=frozenset({"-e", "--eval", "-p", "--print"}),
)
LAUNCHER = Options(
    re.compile(
        r"^-[qvy]$|^--(?:quiet|verbose|yes|no|frozen|locked|offline|isolated|no-sync|no-project|no-cache|refresh"
        r"|exact|active|no-dev|no-editable|all-extras|all-groups|no-config|silent|ignore-scripts)$"
    ),
    valued=frozenset(
        {
            "-p",
            "-c",
            "-w",
            "--package",
            "--call",
            "--python",
            "--with",
            "--with-editable",
            "--with-requirements",
            "--group",
            "--only-group",
            "--extra",
            "--directory",
            "--project",
            "--env-file",
            "--index",
            "--filter",
            "--workspace",
            "--cwd",
        }
    ),
)
INTERPRETER_OPTIONS = {
    "python": PYTHON,
    "pypy": PYTHON,
    "node": NODE,
    "tsx": NODE,
    "ts-node": NODE,
    "ruby": RUBY,
    "perl": PERL,
    "bun": BUN,
} | dict.fromkeys(SHELLS, SHELL)
LAUNCHER_OPTIONS = {"deno": DENO}

SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "AUTH", "CREDENTIAL", "SESSION", "COOKIE")
SECRET_NAME_WORDS = frozenset({"PAT", "PASS", "PWD"})
QUOTED_VALUE = r"'[^']*'|\"[^\"]*\"|[^\s`'\"]+"
SECRET_FLAG_NAME = (
    r"--?(?:p|pass|pat|key|auth|[A-Za-z0-9-]*(?:token|password|passwd|secret|api[-_]?key|credential)[A-Za-z0-9-]*)"
)
QUOTED_ASSIGNED = re.compile(r"(['\"])([A-Za-z_][A-Za-z0-9_]*=)[^'\"]*\1")
ASSIGNED = re.compile(rf"(?<![\w-])([A-Za-z_][A-Za-z0-9_]*)=({QUOTED_VALUE})")
SECRET_FLAG = re.compile(rf"(?<!\S)({SECRET_FLAG_NAME})(=|\s+)(?!-)({QUOTED_VALUE})", re.IGNORECASE)
FLAG_ELEMENT = re.compile(rf"^{SECRET_FLAG_NAME}$", re.IGNORECASE)
FLAG_ASSIGNMENT = re.compile(rf"^({SECRET_FLAG_NAME})=", re.IGNORECASE)
QUOTED_AUTH_HEADER = re.compile(r"(['\"])(authorization:\s*(?:bearer|basic|token)?\s*)[^'\"]*\1", re.IGNORECASE)
AUTH_HEADER = re.compile(
    rf"\b(authorization:\s*(?:bearer|basic|token)?\s*|bearer\s+|basic\s+)({QUOTED_VALUE})", re.IGNORECASE
)
AUTH_ELEMENT = re.compile(r"^(authorization:\s*(?:bearer|basic|token)?\s*|bearer\s+|basic\s+).+$", re.IGNORECASE)
URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)
URL_ELEMENT = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@]+@", re.IGNORECASE)
HEX_RUN = re.compile(r"\b[0-9a-fA-F]{32,}\b")
OPAQUE_RUN = re.compile(r"(?=[A-Za-z0-9+_=-]*\d)(?=[A-Za-z0-9+_=-]*[A-Za-z])[A-Za-z0-9+_=-]{32,}")
MASK = "***"


def words(segment: str) -> list[str]:
    tokens = [stripped for token in segment.split() if (stripped := token.strip(STRIPPED))]
    while tokens:
        match tokens:
            case [head, *rest] if ASSIGNMENT.match(head) or PurePath(head).name in WRAPPERS:
                tokens = rest
            case [head, *rest] if (
                PurePath(head).name in SHELLS
                and (flag := next((index for index, token in enumerate(rest) if command_flag(token)), None)) is not None
            ):
                tokens = rest[flag + 1 :]
            case _:
                return tokens
    return tokens


def command_flag(token: str) -> bool:
    return token.startswith("-") and not token.startswith("--") and "c" in token


def segments(command: str) -> list[list[str]]:
    return [tokens for part in SEGMENT_BREAK.split(command) if (tokens := words(part))]


def stem(token: str) -> str:
    return PurePath(token).stem.lower()


def vocabulary(token: str) -> set[str]:
    return {word for part in PurePath(token.lower()).parts for word in WORD_BREAK.split(part) if word}


def operands(args: list[str]) -> list[str]:
    return [arg for arg in args if not arg.startswith("-")]


def family(program: str) -> str:
    return match.group(1) if (match := VERSIONED.match(program)) else program


def launched(options: Options, args: list[str]) -> Launch | str | None:
    while args:
        match args:
            case [flag, *_] if flag in options.inline:
                return INLINE_CODE
            case [flag, module, *rest] if flag == options.module:
                return Launch([module, *rest], module=True)
            case [flag, *rest] if options.module is not None and flag.startswith(options.module) and len(flag) > 2:
                return Launch([flag[2:], *rest], module=True)
            case [flag, _, *rest] if flag in options.valued:
                args = rest
            case [flag, *rest] if (
                options.switches.match(flag)
                or (flag.startswith("--") and "=" in flag)
                or (flag.startswith(options.attached) and len(flag) > 2)
            ):
                args = rest
            case [flag, *_] if flag.startswith("-") and flag != "-":
                return UNKNOWN_OPTION
            case _:
                return Launch(args)
    return None


def launcher_args(program: str, args: list[str]) -> list[str] | None:
    match program:
        case _ if program in EXECUTORS:
            return args
        case _ if args[:1] and args[0] in EXECUTOR_VERBS.get(program, set()):
            return args[1:]
        case _ if args[:1] and args[0] == RUN_VERBS.get(program):
            return args[1:]
        case _:
            return None


def launcher_reason(program: str, args: list[str]) -> str | None:
    if (inner := launcher_args(program, args)) is None:
        return None
    match launched(LAUNCHER_OPTIONS.get(program, LAUNCHER), inner):
        case str() as reason:
            return reason
        case Launch(tokens=tokens):
            return segment_reason(tokens)
        case _:
            return None


def interpreter_reason(program: str, args: list[str]) -> str | None:
    if (options := INTERPRETER_OPTIONS.get(family(program))) is None:
        return None
    match launched(options, args):
        case str() as reason:
            return reason
        case Launch(tokens=tokens, module=True):
            return segment_reason(tokens)
        case Launch(tokens=[script, *_]) if vocabulary(script) & SCRIPT_WORDS:
            return "runs a deploy, release, or migration script"
        case _:
            return None


def segment_reason(tokens: list[str]) -> str | None:
    program, args = PurePath(tokens[0]).name.lower(), [arg.lower() for arg in tokens[1:]]
    if (reason := launcher_reason(program, tokens[1:])) is not None:
        return reason
    if (reason := interpreter_reason(program, tokens[1:])) is not None:
        return reason
    match program:
        case _ if stem(program) in DEPLOY_WORDS or vocabulary(program) & {"deploy", "release"}:
            return "runs a deploy, release, or publish script"
        case "terraform" | "tofu" | "pulumi" if {"apply", "destroy", "up", "import"} & set(args):
            return "applies infrastructure changes"
        case "kubectl" if KUBECTL_VERBS & set(args):
            return "changes a Kubernetes cluster"
        case "helm" if HELM_VERBS & set(args):
            return "changes a Helm release"
        case "gh" if "release" in args:
            return "cuts a GitHub release"
        case "twine" if "upload" in args:
            return "publishes a package"
        case _ if program in PACKAGE_PUBLISHERS and "publish" in args:
            return "publishes a package"
        case "docker" | "podman" if "push" in args:
            return "pushes a container image"
        case _ if program in CLOUD_CLIS and CLOUD_VERBS & set(args):
            return "deploys to a cloud provider"
        case "git" if "push" in args:
            return "pushes to a git remote"
        case "gt" if "submit" in args:
            return "submits a Graphite stack"
        case "ccx" if "vcs" in args and {"ship", "push"} & set(args):
            return "ships a version-control change"
        case _ if program in DB_CLIENTS:
            return "talks to a database"
        case _ if program in MIGRATORS:
            return "migrates a database"
        case "prisma" if {"migrate", "db"} & set(args):
            return "migrates a database"
        case "rails" | "rake" if any(arg.startswith("db:") for arg in args):
            return "migrates a database"
        case "launchctl":
            return "changes a system service"
        case "brew" if {"install", "upgrade", "uninstall", "reinstall"} & set(args):
            return "changes installed software"
        case "pip" | "pip3" if {"install", "uninstall"} & set(args):
            return "changes installed packages"
        case "uv" if {"sync", "add", "remove"} & set(args) or {"pip", "install"} <= set(args):
            return "changes installed packages"
        case _ if program in RUNNERS and any(vocabulary(arg) & DEPLOY_WORDS for arg in operands(args)):
            return "runs a deploy, release, or publish task"
        case _:
            return None


def excluded(commands: Sequence[str]) -> str | None:
    return next(
        (reason for command in commands for tokens in segments(command) if (reason := segment_reason(tokens))),
        None,
    )


def secret_name(name: str) -> bool:
    upper = name.upper()
    return (
        upper.startswith("AWS_")
        or any(part in upper for part in SECRET_NAME_PARTS)
        or bool(SECRET_NAME_WORDS & set(upper.split("_")))
    )


def mask_assignment(match: re.Match[str]) -> str:
    return f"{match.group(1)}={MASK}" if secret_name(match.group(1)) else match.group(0)


def mask_quoted_assignment(match: re.Match[str]) -> str:
    quote, assignment = match.groups()
    return f"{quote}{assignment}{MASK}{quote}" if secret_name(assignment[:-1]) else match.group(0)


def reads_elsewhere(flag: str) -> bool:
    return flag.lower().endswith(("-stdin", "-file"))


def mask_flag(match: re.Match[str]) -> str:
    flag, separator, _ = match.groups()
    return match.group(0) if reads_elsewhere(flag) else f"{flag}{separator}{MASK}"


def redact(text: str) -> str:
    masked = QUOTED_ASSIGNED.sub(mask_quoted_assignment, text)
    masked = ASSIGNED.sub(mask_assignment, masked)
    masked = SECRET_FLAG.sub(mask_flag, masked)
    masked = QUOTED_AUTH_HEADER.sub(rf"\1\2{MASK}\1", masked)
    masked = AUTH_HEADER.sub(rf"\1{MASK}", masked)
    masked = URL_USERINFO.sub(rf"\1{MASK}@", masked)
    masked = HEX_RUN.sub(MASK, masked)
    masked = OPAQUE_RUN.sub(MASK, masked)
    return masked.replace(str(Path.home()), "~")


def redact_element(arg: str) -> str:
    if (assignment := ASSIGNMENT.match(arg)) is not None:
        return f"{assignment.group(0)}{MASK}" if secret_name(assignment.group(0)[:-1]) else redact(arg)
    if (flag := FLAG_ASSIGNMENT.match(arg)) is not None and not reads_elsewhere(flag.group(1)):
        return f"{flag.group(1)}={MASK}"
    if (header := AUTH_ELEMENT.match(arg)) is not None:
        return f"{header.group(1)}{MASK}"
    return redact(URL_ELEMENT.sub(rf"\1{MASK}@", arg))


def redact_argv(argv: Sequence[str]) -> str:
    masked: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next and not arg.startswith("-"):
            masked.append(MASK)
            hide_next = False
            continue
        hide_next = FLAG_ELEMENT.match(arg) is not None and not reads_elsewhere(arg)
        masked.append(redact_element(arg))
    return " ".join(masked)
