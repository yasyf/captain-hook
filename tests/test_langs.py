from __future__ import annotations

import re

from captain_hook.ast_grep import EXT_TO_LANG
from captain_hook.doc_conventions import DOC_SIBLINGS
from captain_hook.langs import LANG_GLOBS

LEGACY_EXT_TO_LANG = {
    "py": "py",
    "pyi": "py",
    "ts": "ts",
    "tsx": "tsx",
    "js": "js",
    "mjs": "js",
    "cjs": "js",
    "jsx": "js",
    "go": "go",
    "rs": "rs",
    "java": "java",
    "sh": "bash",
    "bash": "bash",
}


def test_table_invariants() -> None:
    assert {ext: EXT_TO_LANG[ext] for ext in LEGACY_EXT_TO_LANG} == LEGACY_EXT_TO_LANG
    assert EXT_TO_LANG["ts"] != EXT_TO_LANG["tsx"]
    assert EXT_TO_LANG["yaml"] == EXT_TO_LANG["yml"] == "yaml"
    assert EXT_TO_LANG["c"] == EXT_TO_LANG["h"] == "c"
    globs = [glob for lang_globs in LANG_GLOBS.values() for glob in lang_globs]
    assert all(re.fullmatch(r"\*\.[A-Za-z0-9_+-]+", glob) for glob in globs)
    assert len(globs) == len(set(globs))
    assert set(DOC_SIBLINGS) == set(LANG_GLOBS)
