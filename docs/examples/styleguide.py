from __future__ import annotations

import ast
from collections.abc import Iterator

from captain_hook import Allow, Input, StyleDiffRule, StyleRule, Violation, Warn, styleguide


class NoPrint(StyleRule):
    """
    print() calls don't belong in committed code:
      - {violations}

    Use a logger (logger.info(...)) instead.
    """

    trigger = "print"
    tests = {
        Input(file="app.py", content="def f():\n    print('debug')\n"): Warn(),
        Input(file="app.py", content="def f():\n    logger.info('ok')\n"): Allow(),
    }

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        for node in ast.walk(tree):
            match node:
                case ast.Call(func=ast.Name(id="print")):
                    yield Violation(node.lineno, "print() call")


class NoBareExcept(StyleRule):
    """
    Bare `except:` swallows every error, including KeyboardInterrupt:
      - {violations}

    Catch a specific exception type instead.
    """

    trigger = "except"
    tests = {
        Input(file="app.py", content="try:\n    f()\nexcept:\n    pass\n"): Warn(),
        Input(file="app.py", content="try:\n    f()\nexcept ValueError:\n    pass\n"): Allow(),
    }

    def check(self, tree: ast.Module) -> Iterator[Violation]:
        yield from (
            Violation(node.lineno, "bare except")
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and node.type is None
        )


class NoNewWildcardImport(StyleDiffRule):
    """
    Wildcard import added by this edit:
      - {violations}

    Import the names you use explicitly instead of `import *`.
    """

    tests = {
        Input(file="m.py", old="import os\n", content="from os import *\n"): Warn(),
        Input(file="m.py", old="from os import *\n", content="from os import *\nx = 1\n"): Allow(),
    }

    def check(self, pre: ast.Module, post: ast.Module) -> Iterator[Violation]:
        old = {node.module for node in ast.walk(pre) if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)}
        yield from (
            Violation(node.lineno, f"from {node.module} import *")
            for node in ast.walk(post)
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names) and node.module not in old
        )


styleguide(NoPrint, NoBareExcept, NoNewWildcardImport)
