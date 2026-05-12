from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

import tree_sitter_bash as tsbash  # type: ignore[import-untyped]
from tree_sitter import Language, Node, Parser

if TYPE_CHECKING:
    from collections.abc import Iterator

BASH_LANGUAGE = Language(tsbash.language())  # pyright: ignore[reportDeprecated]
BASH_PARSER = Parser(BASH_LANGUAGE)

COMPOUND_OPS = frozenset({"&&", "||", ";", "|", "&"})


@dataclass(frozen=True)
class Redirect:
    """A shell redirect parsed from a bash command (e.g. ``> file.txt``, ``2>&1``)."""

    op: str
    target: str
    fd: int | None = None


@dataclass(frozen=True)
class Command:
    """A single parsed shell command with executable, arguments, env vars, and redirects.

    Use ``Command.parse(raw)`` to parse a command string, or access via ``CommandLine``.
    """

    raw: str
    executable: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    redirects: tuple[Redirect, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> Command:
        return CommandLine.parse(raw).primary

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
    parts: tuple[tuple[Command, str | None], ...]

    @classmethod
    def parse(cls, raw: str) -> CommandLine:
        tree = BASH_PARSER.parse(raw.encode())
        parts = cls.walk_node(tree.root_node)
        return cls(raw=raw, parts=tuple(parts)) if parts else cls(raw=raw, parts=((cls.fallback(raw), None),))

    @cached_property
    def commands(self) -> tuple[Command, ...]:
        return tuple(cmd for cmd, _ in self.parts)

    @cached_property
    def primary(self) -> Command:
        return self.parts[-1][0] if self.parts else Command(raw="", executable="", args=())

    @cached_property
    def head(self) -> Command:
        return self.parts[0][0] if self.parts else Command(raw="", executable="", args=())

    def __iter__(self) -> Iterator[Command]:
        return iter(self.commands)

    def __len__(self) -> int:
        return len(self.parts)

    def __str__(self) -> str:
        return self.raw

    def __contains__(self, item: str) -> bool:
        return item in self.raw

    def __bool__(self) -> bool:
        return bool(self.parts)

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
    def extract_command(node: Node) -> Command:
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

        return Command(
            raw=CommandLine.node_text(node),
            executable=executable,
            args=tuple(args),
            env=tuple(env),
            redirects=tuple(redirects),
        )

    @staticmethod
    def collect_parts(children: list[Node], ops: frozenset[str]) -> list[tuple[Command, str | None]]:
        parts: list[tuple[Command, str | None]] = []
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
    def walk_redirected(node: Node) -> list[tuple[Command, str | None]]:
        redirects: list[Redirect] = []
        inner_parts: list[tuple[Command, str | None]] = []
        for child in node.children:
            if child.type == "file_redirect":
                redirects.append(CommandLine.extract_redirect(child))
            else:
                inner_parts.extend(CommandLine.walk_node(child))
        if redirects and inner_parts:
            inner_parts = [
                (
                    Command(
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
                Command(
                    raw=CommandLine.node_text(node),
                    executable="",
                    args=(),
                    redirects=tuple(redirects),
                ),
                None,
            )
        ]

    @staticmethod
    def walk_node(node: Node) -> list[tuple[Command, str | None]]:
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
                parts: list[tuple[Command, str | None]] = []
                for child in node.children:
                    parts.extend(CommandLine.walk_node(child))
                return parts

    @staticmethod
    def fallback(raw: str) -> Command:
        return Command(raw=raw, executable=raw.split()[0] if raw.split() else raw, args=())
