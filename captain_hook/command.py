from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

import tree_sitter_bash as tsbash  # type: ignore[import-untyped]
from loguru import logger
from tree_sitter import Language, Node, Parser

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BASH_PARSER = Parser(Language(tsbash.language()))  # pyright: ignore[reportDeprecated]


@dataclass(frozen=True)
class Redirect:
    """A shell redirect parsed from a bash command (e.g. ``> file.txt``, ``2>&1``)."""

    op: str
    target: str
    fd: int | None = None


@dataclass(frozen=True)
class ParsedCommand:
    """A single parsed shell command with executable, arguments, env vars, and redirects.

    Use ``ParsedCommand.parse(raw)`` to parse a command string, or access via ``CommandLine``.
    """

    raw: str
    executable: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    redirects: tuple[Redirect, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> ParsedCommand:
        return CommandLine.parse(raw).primary

    @classmethod
    def empty(cls) -> ParsedCommand:
        return cls(raw="", executable="", args=())

    @cached_property
    def argv(self) -> tuple[str, ...]:
        return (self.executable, *self.args) if self.executable else ()

    @cached_property
    def program(self) -> str:
        if self.executable == "uv" and len(self.args) >= 2 and self.args[0] == "run":
            return self.args[1]
        if re.match(r"python3?$", self.executable) and len(self.args) >= 2 and self.args[0] == "-m":
            return self.args[1]
        return self.executable

    @cached_property
    def env_dict(self) -> dict[str, str]:
        return dict(self.env)

    def matches(self, pattern: str) -> bool:
        return bool(re.search(pattern, str(self)))

    def has_arg(self, *patterns: str) -> bool:
        return any(re.search(p, a) for p in patterns for a in self.args)

    def __str__(self) -> str:
        return " ".join(self.argv) if self.argv else self.raw

    def __contains__(self, item: str) -> bool:
        return item in str(self)

    def __bool__(self) -> bool:
        return bool(self.executable)


@dataclass(frozen=True)
class CommandLine:
    """A full parsed bash command line, potentially containing multiple commands joined by operators.

    Use ``CommandLine.parse(raw)`` to parse. Access individual commands via ``.commands``
    or the final command via ``.primary``.
    """

    raw: str
    parts: tuple[tuple[ParsedCommand, str | None], ...]

    @classmethod
    def parse(cls, raw: str) -> CommandLine:
        tree = BASH_PARSER.parse(raw.encode())
        if parts := cls.walk_node(tree.root_node):
            return cls(raw=raw, parts=tuple(parts))
        logger.bind(raw=raw).warning("tree-sitter bash parse produced no commands; falling back to naive split")
        return cls(raw=raw, parts=((cls.fallback(raw), None),))

    @cached_property
    def commands(self) -> tuple[ParsedCommand, ...]:
        return tuple(cmd for cmd, _ in self.parts)

    @cached_property
    def primary(self) -> ParsedCommand:
        return self.parts[-1][0] if self.parts else ParsedCommand.empty()

    @cached_property
    def head(self) -> ParsedCommand:
        return self.parts[0][0] if self.parts else ParsedCommand.empty()

    def __iter__(self) -> Iterator[ParsedCommand]:
        return iter(self.commands)

    def __len__(self) -> int:
        return len(self.parts)

    def __str__(self) -> str:
        return self.raw

    def __contains__(self, item: str) -> bool:
        return item in self.raw

    def __bool__(self) -> bool:
        return bool(self.parts)

    @cached_property
    def q(self) -> CommandLineQuery:
        return CommandLineQuery(self)

    def matches(self, pattern: str) -> bool:
        """Whether ``pattern`` matches this command line structurally (ast-grep over tree-sitter-bash).

        The structural counterpart to the regex helpers — ``"cat $$$ARGS"`` matches a ``cat`` call
        however its arguments are spelled. Compose it into a condition with ``CustomCommandLineCondition``.
        """
        from captain_hook import ast_grep

        return ast_grep.matches(self.raw, "bash", pattern)

    def rewrite(self, pattern: str, replace: str) -> str:
        """Rewrite every structural ``pattern`` match to ``replace``, an ast-grep ``$VAR`` fix template.

        ``"cat $$$ARGS"`` → ``"bat $$$ARGS"`` rewrites the call while preserving its arguments; returns
        the command line unchanged when nothing matches.
        """
        from captain_hook import ast_grep

        return ast_grep.rewrite(self.raw, "bash", pattern, replace)

    def capture(self, pattern: str) -> dict[str, str] | None:
        """Match ``pattern`` structurally and return its named metavars, or ``None`` when it doesn't match.

        ``"sed -n $R $F"`` over ``"sed -n '10,40p' f.go"`` yields ``{"R": "10,40p", "F": "f.go"}`` —
        ``$NAME`` captures a single token, ``$$$NAME`` the original-source span of its matches. A pattern
        with no metavars that still matches returns an empty dict.
        """
        from captain_hook import ast_grep

        return ast_grep.capture(self.raw, "bash", pattern)

    @staticmethod
    def node_text(node: Node) -> str:
        return node.text.decode() if node.text else ""

    @staticmethod
    def word_text(node: Node) -> str:
        return (
            CommandLine.node_text(node).strip("'\"")
            if node.type in ("string", "raw_string")
            else CommandLine.node_text(node)
        )

    @staticmethod
    def extract_redirect(node: Node) -> Redirect:
        op = ""
        target = ""
        fd: int | None = None

        for child in node.children:
            match child.type:
                case "file_descriptor":
                    fd = int(CommandLine.node_text(child)) if CommandLine.node_text(child).isdigit() else None
                case t if t in (">", ">>", "<", "<<", ">&", "<&", ">|"):
                    op = t
                case _:
                    text = CommandLine.node_text(child)
                    if not op and text in (">", ">>", "<", "<<", ">&", "<&", ">|"):
                        op = text
                    elif op:
                        target = text
                    else:
                        target = text

        return Redirect(op=op, target=target, fd=fd)

    @staticmethod
    def extract_command(node: Node) -> ParsedCommand:
        executable = ""
        args: list[str] = []
        env: list[tuple[str, str]] = []
        redirects: list[Redirect] = []

        for child in node.children:
            match child.type:
                case "command_name":
                    executable = CommandLine.word_text(child)
                case "variable_assignment":
                    name = next((c for c in child.children if c.type == "variable_name"), None)
                    val = child.children[-1] if len(child.children) >= 3 else None
                    if name:
                        env.append(
                            (
                                CommandLine.node_text(name),
                                CommandLine.word_text(val) if val and val.type != "=" else "",
                            )
                        )
                case "file_redirect":
                    redirects.append(CommandLine.extract_redirect(child))
                case _ if child.type in (
                    "word",
                    "string",
                    "raw_string",
                    "number",
                    "concatenation",
                    "simple_expansion",
                    "expansion",
                ):
                    if executable:
                        args.append(CommandLine.word_text(child))
                    else:
                        executable = CommandLine.word_text(child)
                case _:
                    pass

        return ParsedCommand(
            raw=CommandLine.node_text(node),
            executable=executable,
            args=tuple(args),
            env=tuple(env),
            redirects=tuple(redirects),
        )

    @staticmethod
    def collect_parts(children: list[Node], ops: frozenset[str]) -> list[tuple[ParsedCommand, str | None]]:
        parts: list[tuple[ParsedCommand, str | None]] = []
        for child in children:
            text = CommandLine.node_text(child)
            if child.type in ops or text in ops:
                if parts:
                    cmd, _ = parts[-1]
                    parts[-1] = (cmd, text)
                continue
            if sub := CommandLine.walk_node(child):
                parts.extend(sub)
        return parts

    @staticmethod
    def walk_redirected(node: Node) -> list[tuple[ParsedCommand, str | None]]:
        redirects: list[Redirect] = []
        inner_parts: list[tuple[ParsedCommand, str | None]] = []
        for child in node.children:
            if child.type == "file_redirect":
                redirects.append(CommandLine.extract_redirect(child))
            else:
                inner_parts.extend(CommandLine.walk_node(child))
        if redirects and inner_parts:
            inner_parts = [
                (
                    ParsedCommand(
                        raw=cmd.raw,
                        executable=cmd.executable,
                        args=cmd.args,
                        env=cmd.env,
                        redirects=(*cmd.redirects, *redirects),
                    ),
                    op,
                )
                for cmd, op in inner_parts
            ]
        return inner_parts or [
            (
                ParsedCommand(
                    raw=CommandLine.node_text(node),
                    executable="",
                    args=(),
                    redirects=tuple(redirects),
                ),
                None,
            )
        ]

    @staticmethod
    def walk_node(node: Node) -> list[tuple[ParsedCommand, str | None]]:
        match node.type:
            case "program":
                return CommandLine.collect_parts(node.children, frozenset({";"}))
            case "list":
                return CommandLine.collect_parts(node.children, COMPOUND_OPS)
            case "pipeline":
                return CommandLine.collect_parts(node.children, frozenset({"|"}))
            case "command":
                return [(CommandLine.extract_command(node), None)]
            case "redirected_statement":
                return CommandLine.walk_redirected(node)
            case _:
                parts: list[tuple[ParsedCommand, str | None]] = []
                for child in node.children:
                    parts.extend(CommandLine.walk_node(child))
                return parts

    @staticmethod
    def fallback(raw: str) -> ParsedCommand:
        return ParsedCommand(raw=raw, executable=raw.split()[0] if raw.split() else raw, args=())


@dataclass(frozen=True)
class CommandLineQuery:
    """Predicate helpers for inspecting a parsed ``CommandLine``.

    Wraps a ``CommandLine`` to answer common yes/no questions a hook condition
    needs — which executable runs, whether a subcommand or token appears, or
    whether the line redirects/pipes. Obtain one via ``CommandLine.q``.
    """

    line: CommandLine

    def runs(self, *argv: str) -> bool:
        """Return whether the primary command's argv starts with ``argv``.

        Args:
            *argv: Leading argv tokens to match, e.g. ``("git", "push")``.

        Returns:
            ``True`` if ``argv`` is non-empty and is a prefix of the primary
            command's ``argv``.
        """
        return bool(argv) and self.line.primary.argv[: len(argv)] == argv

    def has_subcommand(self, name: str) -> bool:
        """Return whether any command in the line carries ``name`` as an argument.

        Args:
            name: The subcommand/argument token to look for (e.g. ``"push"``).

        Returns:
            ``True`` if ``name`` appears in the arguments of any parsed command.
        """
        return any(name in cmd.args for cmd in self.line.commands)

    def any_command(self, pred: Callable[[ParsedCommand], bool]) -> bool:
        """Return whether any command in the line satisfies ``pred``.

        Args:
            pred: Predicate applied to each parsed ``ParsedCommand``.

        Returns:
            ``True`` if ``pred`` returns truthy for at least one command.
        """
        return any(pred(cmd) for cmd in self.line.commands)

    def uses_redirect(self) -> bool:
        """Return whether the line redirects output or pipes between commands.

        Returns:
            ``True`` if any command has a file redirect or the parts are joined
            by a pipe (``|``) operator.
        """
        return any(cmd.redirects for cmd in self.line.commands) or any(op == "|" for _, op in self.line.parts if op)

    def contains_token(self, token: str) -> bool:
        """Return whether ``token`` appears as a whole argv element in any command.

        Unlike ``has_subcommand`` this matches the executable as well as the
        arguments and requires an exact element match, not a substring.

        Args:
            token: The exact argv token to look for.

        Returns:
            ``True`` if ``token`` equals an argv element of any parsed command.
        """
        return any(token == a for cmd in self.line.commands for a in cmd.argv)
