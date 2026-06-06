from __future__ import annotations

import ast

from captain_hook import Allow, Input, Warn
from captain_hook.style import StyleDiffRule, StyleRule, matchers as M, styleguide


class NoPrint(StyleRule):
    """
    print() calls don't belong in committed code:
      - {violations}

    Use a logger (logger.info(...)) instead.
    """

    tests = {
        Input(file="app.py", content="def f():\n    print('debug')\n"): Warn(),
        Input(file="app.py", content="def f():\n    logger.info('ok')\n"): Allow(),
    }
    match = M.calls("print")
    label = "print() call"


class NoBareExcept(StyleRule):
    """
    Bare `except:` swallows every error, including KeyboardInterrupt:
      - {violations}

    Catch a specific exception type instead.
    """

    tests = {
        Input(file="app.py", content="try:\n    f()\nexcept:\n    pass\n"): Warn(),
        Input(file="app.py", content="try:\n    f()\nexcept ValueError:\n    pass\n"): Allow(),
    }
    match = M.kind(ast.ExceptHandler).where(lambda n: n.type is None)
    label = "bare except"


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
    match = M.imports.where(lambda n: any(alias.name == "*" for alias in n.names))


styleguide(NoPrint, NoBareExcept, NoNewWildcardImport)
