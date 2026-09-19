from __future__ import annotations

import ast
import re

from captain_hook import Allow, Input, Warn, lint

_CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_prompt_load(call: ast.Call) -> bool:
    # `NAME = Prompt.load(...)` is this repo's accepted exception: the name documents
    # intent for a call site that appears well below the imports, before it's written.
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == "load") or (
        isinstance(func, ast.Name) and func.id == "load"
    )


def _reads_better_named(value: ast.expr) -> bool:
    match value:
        case ast.Constant(value=str() as text):
            return "\n" in text or len(text) > 100
        case ast.JoinedStr(values=parts):
            return any(map(_reads_better_named, parts))
        case ast.BinOp(left=left, right=right):
            return _reads_better_named(left) or _reads_better_named(right)
        case ast.Dict() | ast.List() | ast.Tuple() | ast.Set() | ast.Call(func=ast.Name(id="frozenset")):
            return value.end_lineno - value.lineno >= 3
        case _:
            return False


def single_use_constants(content: str) -> list[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    exported: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
        ):
            exported = {
                elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }

    violations = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if not _CONST_NAME.match(name) or name in exported:
            continue
        if isinstance(node.value, ast.Call) and _is_prompt_load(node.value):
            continue
        if _reads_better_named(node.value):
            continue
        if len(re.findall(rf"\b{name}\b", content)) == 2:
            violations.append(f"{name} (line {node.targets[0].lineno})")
    return violations


lint(
    single_use_constants,
    message=(
        'User feedback: "dont do constants like DANGEROUS, just inline" plus repeated plan corrections '
        "dropping module-level lib.py builders and Signals(...) constants for inline literals at the one "
        "call site. This constant is defined once and used exactly once elsewhere in the file -- inline it "
        "at its call site instead: {violations}"
    ),
    tests={
        Input(
            file="captain_hook/util/proc.py",
            content=(
                "import re\n\n"
                "DANGEROUS = re.compile(r'rm -rf')\n\n"
                "def is_dangerous(cmd: str) -> bool:\n"
                "    return bool(DANGEROUS.search(cmd))\n"
            ),
        ): Warn(pattern="DANGEROUS"),
        Input(
            file="captain_hook/util/proc.py",
            content=(
                "import re\n\n"
                "DANGEROUS = re.compile(r'rm -rf')\n\n"
                "def is_dangerous(cmd: str) -> bool:\n"
                "    return bool(DANGEROUS.search(cmd))\n\n"
                "def describe(cmd: str) -> str:\n"
                "    return 'dangerous' if DANGEROUS.search(cmd) else 'safe'\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/packs/general/models.py",
            content=(
                "from captain_hook import Prompt, llm_nudge\n\n"
                "IMPLEMENTATION_SPAWN_NUDGE = Prompt.load('models/implementation_spawn_nudge')\n\n"
                "llm_nudge(IMPLEMENTATION_SPAWN_NUDGE, message='go')\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/packs/general/tools.py",
            content=(
                "DEFAULT_TIMEOUT = 30\n\n"
                "__all__ = ['DEFAULT_TIMEOUT']\n\n"
                "def call(timeout=None):\n"
                "    return timeout or DEFAULT_TIMEOUT\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/util/proc.py",
            content="pattern = 'rm -rf'\n\ndef check(cmd):\n    return pattern in cmd\n",
        ): Allow(),
        Input(
            file="captain_hook/review/store.py",
            content=(
                "import sqlite3\n\n"
                'REVIEW_DDL = """\n'
                "CREATE TABLE review (\n"
                "  id TEXT PRIMARY KEY\n"
                ");\n"
                '"""\n\n'
                "def migrate(db: sqlite3.Connection) -> None:\n"
                "    db.executescript(REVIEW_DDL)\n"
            ),
        ): Allow(),
        Input(
            file="stress/shims.py",
            content=(
                "def venv_bin() -> str:\n    return '/tmp/venv/bin'\n\n"
                'CAPT_HOOK_SHIM = f"""#!/bin/sh\n'
                'exec "{venv_bin()}/capt-hook" "$@"\n'
                '"""\n\n'
                "def write(path: str) -> None:\n"
                "    open(path, 'w').write(CAPT_HOOK_SHIM)\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/review/store.py",
            content=(
                "BASE_DDL = 'PRAGMA journal_mode=wal;\\n'\n\n"
                'REVIEW_DDL = BASE_DDL + """\n'
                "CREATE TABLE review (id TEXT PRIMARY KEY);\n"
                '"""\n\n'
                "def migrate(db) -> None:\n"
                "    db.executescript(REVIEW_DDL)\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/review/dashboard.py",
            content=(
                "KIND_STYLE = {\n"
                "    'create': 'green',\n"
                "    'extend': 'cyan',\n"
                "    'fix': 'yellow',\n"
                "}\n\n"
                "def style(kind: str) -> str:\n"
                "    return KIND_STYLE[kind]\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/review/judge.py",
            content=(
                "JUDGE_ROLE = 'You grade one correction at a time for durability, and you answer with a verdict "
                "and nothing else at all.'\n\n"
                "def role() -> str:\n"
                "    return JUDGE_ROLE\n"
            ),
        ): Allow(),
        Input(
            file="captain_hook/util/http.py",
            content="BACKOFF_BASE = 0.5\n\ndef delay(attempt: int) -> float:\n    return BACKOFF_BASE * attempt\n",
        ): Warn(pattern="BACKOFF_BASE"),
        Input(
            file="captain_hook/util/runtimes.py",
            content=(
                "JS_RUNTIMES = ('node', 'bun', 'deno')\n\n"
                "def is_js(cmd: str) -> bool:\n"
                "    return cmd in JS_RUNTIMES\n"
            ),
        ): Warn(pattern="JS_RUNTIMES"),
    },
)
