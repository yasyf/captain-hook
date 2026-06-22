from __future__ import annotations

import ast

from captain_hook import Allow, Input, Warn
from captain_hook.style import StyleRule, ast_grep_diff_rule, ast_grep_rule, styleguide
from captain_hook.style import matchers as M

NoPrint = ast_grep_rule(
    "NoPrint",
    pattern="print($$$)",
    message="""
    print() calls don't belong in committed code:
      - {violations}

    Use a logger (logger.info(...)) instead.
    """,
    label="print() call",
    tests={
        Input(file="app.py", content="def f():\n    print('debug')\n"): Warn(),
        Input(file="app.py", content="def f():\n    logger.info('ok')\n"): Allow(),
    },
)


NoNewWildcardImport = ast_grep_diff_rule(
    "NoNewWildcardImport",
    pattern="from $MOD import *",
    message="""
    Wildcard import added by this edit:
      - {violations}

    Import the names you use explicitly instead of `import *`.
    """,
    tests={
        Input(file="m.py", old="import os\n", content="from os import *\n"): Warn(),
        Input(file="m.py", old="from os import *\n", content="from os import *\nx = 1\n"): Allow(),
    },
)


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
    label = "bare except"
    match = M.kind(ast.ExceptHandler).where(lambda n: isinstance(n, ast.ExceptHandler) and n.type is None)


styleguide(NoPrint, NoNewWildcardImport, NoBareExcept)
