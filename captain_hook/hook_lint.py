"""Lint hook files against the authoring bar: a message states the rule, then the remediation, the
hook code uses the declarative surface instead of hand-rolled parsing, and a mandatory hook stays
evidence-free."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

from captain_hook.types import render_runs, runs_spelling

if TYPE_CHECKING:
    from captain_hook.types import HookResult

MAX_SENTENCES = 2
MAX_CHARS = 300

CODE_SPAN = re.compile(r"`[^`]*`")
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|etc|vs)\.", re.IGNORECASE)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
ECHOED_INPUT = re.compile(r"\{[^{}]*\b(?:reasoning|user_prompt|prompt)\b[^{}]*\}")
RETIRED_ESCAPE = re.compile(
    r"root:raw|(?<![\w=-])tooling-lane:|CAPT_HOOK_CCX_RAW=(?!(?:1|true|yes)\b)[\w-]*", re.IGNORECASE
)

COPY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("quoted text", re.compile(r"""(?<!\w)["“'‘][^"”'’\n]*\s[^"”'’\n]*\s[^"”'’\n]*["”'’](?!\w)""")),
    (
        "provenance",
        re.compile(
            r"\b(?:user|owner)(?:'s)?\s+(?:feedback|said|says|asked|correction|rule|order)\b|\brulings?\b",
            re.IGNORECASE,
        ),
    ),
    ("record id", re.compile(r"(?<![\w-])[RGL]\d{1,4}\b|(?<![\w/&])#\d{2,}\b|\bwf_[0-9a-f]{6,}\b")),
    ("session id", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("commit hash", re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")),
    (
        "date or time",
        re.compile(r"\b20\d\d-\d\d-\d\d\b|\b\d{1,2}:\d\d(?::\d\d)?\s?(?:Z|UTC|PT|PDT|PST|am|pm)\b|\b\d\d:\d\d:\d\d\b"),
    ),
    ("token count", re.compile(r"\b\d[\d,.]*\s?[kKmM]?\s+tokens\b")),
    (
        "narrative",
        re.compile(
            r"\b(?:first seen|last time|this happened|happened (?:on|at|when)|was caused by|we (?:saw|hit)"
            r"|incident on|has not (?:replied|answered)|hasn't (?:replied|answered))\b",
            re.IGNORECASE,
        ),
    ),
)

MESSAGE_KEYWORDS = frozenset({"message", "reason", "hint", "note"})
MESSAGE_POSITIONAL = frozenset({"block", "warn", "context", "gate", "nudge", "deny"})
REGEX_METHODS = frozenset({"search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"})
COMMAND_TEXT = re.compile(r"\.raw\b|tool_input(?:\[|\.get\()[\"']command[\"']|^(?:str\()?(?:evt\.)?(?:command|cmd)\)?$")
TRANSCRIPT_FILES = re.compile(r"\.jsonl\b|\.claude/projects")
BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})
COMMAND_PATTERN_CALLS = frozenset({"Command", "CommandCondition", "block_command", "warn_command"})
ESCAPE_MARKERS = re.compile(r"ccx:raw|root:raw|tooling-lane:|^CAPT_HOOK_CCX_RAW$")
TEXT_PARSERS = REGEX_METHODS | frozenset(
    {"compile", "startswith", "endswith", "find", "rfind", "index", "count", "partition", "rpartition", "getenv", "get"}
)
CONTEXT_TOOLS = frozenset({"Agent", "Task", "Skill", "Read", "Grep", "Glob"})
ALLOWED_COMMENT = re.compile(r"#!|#\s*(?:TODO|FIXME|WORKAROUND|noqa|type:|pyright:|ruff:|fmt:|pragma)")
LLM_CALLS = frozenset({"llm_evaluate", "llm", "llm_gate", "llm_nudge", "prompt_check"})
TRANSCRIPT_ATTRIBUTES = frozenset({"t", "transcript"})
MANDATORY_EVIDENCE = (
    "a mandatory hook is evidence-free; move the LLM or transcript check into an advisory hook registered "
    "without mandatory=True"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One way a hook file misses the bar, at the line that misses it."""

    path: Path
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def copy_violations(text: str) -> list[str]:
    """How a hook message misses the copy bar: the rule, then the remediation, in at most two sentences.

    Code spans and ``{placeholders}`` are exempt from the prose rules, so a remediation command may carry
    quotes, ids, and paths of its own.
    """
    prose = ABBREVIATION.sub("eg", PLACEHOLDER.sub("X", CODE_SPAN.sub("X", stripped := text.strip())))
    sentences = [part for part in SENTENCE_BREAK.split(prose) if part.strip()]
    return [
        *([f"{len(sentences)} sentences; state the rule, then the remediation"] * (len(sentences) > MAX_SENTENCES)),
        *([f"{len(stripped)} chars; keep it to {MAX_CHARS}"] * (len(stripped) > MAX_CHARS)),
        *(["echoes the prompt or the judge's reasoning; state the rule instead"] * bool(ECHOED_INPUT.search(stripped))),
        *(
            f"retired escape {match.group(0)!r}; offer `# ccx:raw`, `CAPT_HOOK_CCX_RAW=1`, or a `ccx:` line"
            for match in RETIRED_ESCAPE.finditer(stripped)
        ),
        *(f"{name} {match.group(0)!r}" for name, pattern in COPY_RULES if (match := pattern.search(prose))),
    ]


def result_violations(result: HookResult) -> list[str]:
    """Copy violations in the text a hook result surfaces to the agent: its message, or a rewrite's note."""
    return [violation for text in (result.message, result.note) if text for violation in copy_violations(text)]


def bindings(node: ast.stmt) -> list[tuple[ast.expr, ast.expr]]:
    match node:
        case ast.Assign(targets=targets, value=value):
            return [(target, value) for target in targets]
        case ast.AnnAssign(target=target, value=ast.expr() as value):
            return [(target, value)]
        case _:
            return []


def module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    return {target.id: value for node in tree.body for target, value in bindings(node) if isinstance(target, ast.Name)}


def placeholder(node: ast.expr) -> str:
    return f"{{{ast.unparse(node)}}}"


def fstring_part(part: ast.expr) -> str:
    match part:
        case ast.Constant(value=str() as text):
            return text
        case ast.FormattedValue(value=value):
            return placeholder(value)
        case _:
            return placeholder(part)


def texts(node: ast.expr, consts: dict[str, ast.expr]) -> list[str]:
    match node:
        case ast.Constant(value=str() as text):
            return [text]
        case ast.JoinedStr(values=parts):
            return ["".join(map(fstring_part, parts))]
        case ast.BinOp(op=ast.Add(), left=left, right=right):
            return [
                head + tail
                for head in texts(left, consts) or [placeholder(left)]
                for tail in texts(right, consts) or [placeholder(right)]
            ]
        case ast.Name(id=name) if name in consts:
            return texts(consts.pop(name), consts)
        case ast.Call(func=ast.Attribute(attr="format", value=template)):
            return texts(template, consts)
        case ast.Call(func=ast.Name(id="dedent") | ast.Attribute(attr="dedent"), args=[inner]):
            return [dedent(text).strip() for text in texts(inner, consts)]
        case ast.Lambda(body=body):
            return texts(body, consts)
        case ast.IfExp(body=body, orelse=orelse):
            return texts(body, consts) + texts(orelse, consts)
        case _:
            return []


def callee(call: ast.Call) -> str | None:
    match call.func:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case _:
            return None


def message_nodes(call: ast.Call) -> Iterator[ast.expr]:
    yield from (keyword.value for keyword in call.keywords if keyword.arg in MESSAGE_KEYWORDS)
    if callee(call) in MESSAGE_POSITIONAL and call.args:
        yield call.args[0]


def copy_findings(path: Path, tree: ast.Module) -> Iterator[Finding]:
    consts = module_constants(tree)
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        for message in message_nodes(call):
            for text in texts(message, dict(consts)):
                for violation in copy_violations(text):
                    yield Finding(path, message.lineno, "copy", violation)


def regex_over_command(call: ast.Call) -> bool:
    match call.func:
        case ast.Attribute(attr=method, value=receiver) if method in REGEX_METHODS:
            operands = [receiver, *call.args] if method == "split" else call.args
            return any(COMMAND_TEXT.search(ast.unparse(operand)) for operand in operands)
        case _:
            return False


def textual_command_name(call: ast.Call) -> str | None:
    match call.args:
        case [ast.Constant(value=str() as pattern), *_] if callee(call) in COMMAND_PATTERN_CALLS and (
            argvs := runs_spelling(pattern)
        ):
            return render_runs(argvs)
        case _:
            return None


def code_violation(node: ast.AST) -> str | None:
    match node:
        case ast.Call() if (runs := textual_command_name(node)) is not None:
            return f"matches a command name as text; match it with {runs}"
        case ast.Call(func=ast.Attribute(value=ast.Name(id="shlex"), attr="split")):
            return "splits a command by hand; match with Runs(...) or walk evt.command"
        case ast.Call() if regex_over_command(node):
            return "regexes command text; match with Runs(...), a CommandSchema, or an ast-grep rewrite_command pattern"
        case ast.Attribute(attr="transcript_path") | ast.Name(id="transcript_path"):
            return "reads the transcript by hand; query evt.ctx.t or use RanCommand, UsedTool, UsedSkill"
        case ast.Constant(value=str() as text) if TRANSCRIPT_FILES.search(text):
            return "reads transcript files by hand; query evt.ctx.t or use RanCommand, UsedTool, UsedSkill"
        case ast.ExceptHandler(type=None):
            return "swallows failures; let the hook raise, the dispatcher records the fault"
        case ast.ExceptHandler(type=ast.Name(id=name)) if name in BROAD_EXCEPTIONS:
            return "swallows failures; let the hook raise, the dispatcher records the fault"
        case ast.Call(func=ast.Name(id="suppress") | ast.Attribute(attr="suppress"), args=args) if any(
            isinstance(arg, ast.Name) and arg.id in BROAD_EXCEPTIONS for arg in args
        ):
            return "swallows failures; let the hook raise, the dispatcher records the fault"
        case _:
            return None


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def calls_named(node: ast.AST | None, name: str) -> list[ast.Call]:
    return [] if node is None else [n for n in ast.walk(node) if isinstance(n, ast.Call) and callee(n) == name]


def tool_names(call: ast.Call) -> set[str]:
    return {
        name
        for tool in calls_named(keyword(call, "only_if"), "Tool")
        for arg in tool.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        for name in arg.value.split("|")
    }


def escapable(call: ast.Call) -> bool:
    return keyword(call, "confirm") is not None or any(
        calls_named(keyword(call, field), "Annotated") for field in ("skip_if", "only_if")
    )


def reads_annotations(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "annotations" for n in ast.walk(node))


def unescaped_context_blocks(tree: ast.Module) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        match node:
            case ast.Call() if (
                callee(node) == "hook"
                and isinstance(block := keyword(node, "block"), ast.Constant)
                and block.value is True
                and tool_names(node) & CONTEXT_TOOLS
                and not escapable(node)
            ):
                yield node
            case ast.FunctionDef(decorator_list=decorators):
                for on in (d for d in decorators if isinstance(d, ast.Call) and callee(d) == "on"):
                    if tool_names(on) & CONTEXT_TOOLS and not escapable(on) and not reads_annotations(node):
                        yield from (
                            block
                            for block in calls_named(node, "block")
                            if isinstance(block.func, ast.Attribute) and keyword(block, "confirm") is None
                        )


def parent_map(tree: ast.Module) -> dict[int, ast.AST]:
    return {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def parses_text(node: ast.AST, parent: ast.AST | None) -> bool:
    match parent:
        case ast.Compare():
            return True
        case ast.Call() if callee(parent) in TEXT_PARSERS:
            return True
        case ast.Subscript(value=value) if "environ" in ast.unparse(value):
            return True
        case _:
            return False


def escape_findings(path: Path, tree: ast.Module) -> Iterator[Finding]:
    parents = parent_map(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and ESCAPE_MARKERS.search(node.value)
            and parses_text(node, parents.get(id(node)))
        ):
            yield Finding(path, node.lineno, "code", "parses the ccx escape by hand; match it with Annotated(...)")
    for node in unescaped_context_blocks(tree):
        yield Finding(
            path,
            getattr(node, "lineno", 1),
            "code",
            "blocks a dispatch or read with no escape; add skip_if=[Annotated(...)] or confirm=Confirm(...)",
        )


def code_findings(path: Path, tree: ast.Module) -> Iterator[Finding]:
    for node in ast.walk(tree):
        if (violation := code_violation(node)) is not None:
            yield Finding(path, getattr(node, "lineno", 1), "code", violation)


def registers_mandatory(call: ast.Call, registrars: frozenset[str]) -> bool:
    return callee(call) in registrars or any(
        keyword.arg == "mandatory" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        for keyword in call.keywords
    )


def import_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound by ``import ... as`` or ``from ... import ... as``, mapped to the names they stand for."""
    return {
        alias.asname: alias.name.rsplit(".", 1)[-1]
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
        if alias.asname
    }


def local_registrars(tree: ast.Module) -> frozenset[str]:
    """Module names bound to a registrar carrying ``mandatory=True``: ``guard = partial(on, ..., mandatory=True)``."""
    return frozenset(
        target.id
        for node in tree.body
        for target, value in bindings(node)
        if isinstance(target, ast.Name) and isinstance(value, ast.Call) and registers_mandatory(value, frozenset())
    )


def imported_registrars(path: Path, tree: ast.Module) -> frozenset[str]:
    """Names imported from a sibling module that binds them as mandatory registrars, under their local names."""
    return frozenset(
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
        if (sibling := path.parent / f"{node.module.rsplit('.', 1)[-1]}.py").is_file()
        for exported in [local_registrars(ast.parse(sibling.read_text(), filename=str(sibling)))]
        for alias in node.names
        if alias.name in exported
    )


def mandatory_handlers(
    path: Path, tree: ast.Module, functions: dict[str, ast.FunctionDef]
) -> Iterator[ast.FunctionDef]:
    registrars = local_registrars(tree) | imported_registrars(path, tree)
    for node in ast.walk(tree):
        match node:
            case ast.FunctionDef(decorator_list=decorators) if any(
                isinstance(call, ast.Call) and registers_mandatory(call, registrars) for call in decorators
            ):
                yield node
            case ast.Call(func=ast.Call() as registration, args=[ast.Name(id=name)]) if (
                name in functions and registers_mandatory(registration, registrars)
            ):
                yield functions[name]


def evidence_reads(
    function: ast.FunctionDef, functions: dict[str, ast.FunctionDef], aliases: dict[str, str], seen: set[str]
) -> Iterator[tuple[ast.AST, str]]:
    seen.add(function.name)
    for node in ast.walk(function):
        match node:
            case ast.Call() if (name := aliases.get(called := callee(node) or "", called)) in LLM_CALLS:
                yield node, f"calls {name}"
            case ast.Attribute(attr=attr, value=ast.Attribute(attr="ctx")) if attr in TRANSCRIPT_ATTRIBUTES:
                yield node, f"reads {ast.unparse(node)}"
            case ast.Call() if (name := callee(node)) in functions and name not in seen:
                yield from evidence_reads(functions[name], functions, aliases, seen)


def mandatory_findings(path: Path, tree: ast.Module) -> Iterator[Finding]:
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    aliases = import_aliases(tree)
    for handler in mandatory_handlers(path, tree, functions):
        for node, read in evidence_reads(handler, functions, aliases, set()):
            yield Finding(path, node.lineno, "code", f"mandatory hook {handler.name} {read}; {MANDATORY_EVIDENCE}")


def comment_findings(path: Path, source: str) -> Iterator[Finding]:
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and not ALLOWED_COMMENT.match(token.string):
            yield Finding(path, token.start[0], "comment", "rename or restructure instead of commenting")


def lint_source(path: Path, source: str) -> list[Finding]:
    """Every finding in one hook file's source, ordered by line."""
    tree = ast.parse(source, filename=str(path))
    return sorted(
        [
            *copy_findings(path, tree),
            *code_findings(path, tree),
            *escape_findings(path, tree),
            *mandatory_findings(path, tree),
            *comment_findings(path, source),
        ],
        key=lambda finding: (finding.line, finding.rule, finding.detail),
    )


def is_hook_source(path: Path) -> bool:
    return (
        path.suffix == ".py"
        and "__pycache__" not in path.parts
        and "tests" not in path.parts
        and path.name != "conftest.py"
        and not path.name.startswith("test_")
        and not path.stem.endswith("_test")
    )


def hook_sources(paths: Iterable[Path]) -> list[Path]:
    return sorted(
        {
            source
            for path in paths
            for source in (sorted(path.rglob("*.py")) if path.is_dir() else [path])
            if is_hook_source(source)
        }
    )


def lint_paths(paths: Iterable[Path]) -> list[Finding]:
    """Every finding across the hook files under ``paths``; a directory is walked, test files skipped."""
    return [finding for source in hook_sources(paths) for finding in lint_source(source, source.read_text())]
