from __future__ import annotations

import re

import pytest

from captain_hook.ast_grep import DOC_PREFIXES, EXT_TO_LANG
from captain_hook.langs import COMMENT_TYPES, LANG_GLOBS
from hatch_build import accepts_language, build_comment_types, build_lang_globs

LEGACY_EXT_TO_LANG = {
    "py": "py",
    "pyi": "py",
    "ts": "ts",
    "tsx": "tsx",
    "js": "js",
    "mjs": "js",
    "cjs": "js",
    "jsx": "jsx",
    "go": "go",
    "rs": "rs",
    "java": "java",
    "sh": "bash",
    "bash": "bash",
}


def test_generated_table_is_fresh() -> None:
    message = (
        "generated language data is stale; run `uv run python hatch_build.py`, then "
        "`uv sync --reinstall-package capt-hook`"
    )
    assert build_lang_globs() == LANG_GLOBS, message
    assert build_comment_types() == COMMENT_TYPES, message


def test_table_invariants() -> None:
    assert {ext: EXT_TO_LANG[ext] for ext in LEGACY_EXT_TO_LANG} == LEGACY_EXT_TO_LANG
    assert EXT_TO_LANG["ts"] != EXT_TO_LANG["tsx"]
    assert EXT_TO_LANG["yaml"] == EXT_TO_LANG["yml"] == "yaml"
    assert EXT_TO_LANG["c"] == EXT_TO_LANG["h"] == "c"
    globs = [glob for lang_globs in LANG_GLOBS.values() for glob in lang_globs]
    assert all(re.fullmatch(r"\*\.[A-Za-z0-9_+-]+", glob) for glob in globs)
    assert len(globs) == len(set(globs))
    assert set(DOC_PREFIXES) <= set(LANG_GLOBS)


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_language_probe_propagates_process_interrupts(
    monkeypatch: pytest.MonkeyPatch, signal: type[BaseException]
) -> None:
    def interrupt(_source: str, _alias: str) -> None:
        raise signal

    accepts_language.cache_clear()
    monkeypatch.setattr("hatch_build.SgRoot", interrupt)
    with pytest.raises(signal):
        accepts_language(signal.__name__)
