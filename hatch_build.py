from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib.metadata import version
from pathlib import Path

from ast_grep_py import SgRoot
from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from pygments.lexers import get_all_lexers

GLOB_PATTERN = re.compile(r"^\*\.([A-Za-z0-9_+-]+)$")
COMMENT_KIND_SEEDS = (
    "comment",
    "line_comment",
    "block_comment",
    "multiline_comment",
    "html_comment",
    "js_comment",
)
DOC_KIND_SEEDS = (
    "outer_doc_comment_marker",
    "inner_doc_comment_marker",
    "documentation_block_comment",
)
DECL_KIND_SEEDS: dict[str, tuple[str, ...] | None] = {
    "function_declaration": None,
    "method_declaration": None,
    "type_declaration": None,
    "var_declaration": None,
    "const_declaration": None,
    "package_clause": None,
    "const_spec": None,
    "var_spec": None,
    "field_declaration": None,
    "import_declaration": None,
    "method_elem": None,
    "function_item": None,
    "struct_item": None,
    "enum_item": None,
    "trait_item": None,
    "impl_item": None,
    "type_item": None,
    "const_item": None,
    "static_item": None,
    "mod_item": None,
    "class_declaration": None,
    "method_definition": None,
    "export_statement": None,
    "interface_declaration": None,
    "abstract_class_declaration": None,
    "type_alias_declaration": None,
    "enum_declaration": None,
    "constructor_declaration": None,
    "record_declaration": None,
    "annotation_type_declaration": None,
    "property_declaration": None,
    "object_declaration": None,
    "protocol_declaration": None,
    "struct_declaration": None,
    "namespace_declaration": None,
    "function_definition": ("c", "cpp", "php", "scala", "solidity"),
    "namespace_definition": None,
    "type_definition": None,
    "class_definition": ("scala",),
    "object_definition": None,
    "trait_definition": None,
    "val_definition": None,
    "contract_declaration": None,
    "state_variable_declaration": None,
    "method": ("rb",),
    "singleton_method": ("rb",),
    "class": ("rb",),
    "module": ("rb",),
}
COMMENTLESS_LANGS = frozenset({"md"})
REGEN_COMMAND = "uv run python hatch_build.py"


@contextmanager
def suppress_stderr() -> Iterator[None]:
    saved = os.dup(2)
    try:
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), 2)
            yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


@cache
def accepts_language(alias: str) -> bool:
    with suppress_stderr():
        try:
            SgRoot("", alias)
        # ast-grep raises a pyo3 PanicException outside Exception for unknown languages.
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            return False
    return True


@cache
def defines_kind(lang: str, kind: str) -> bool:
    with suppress_stderr():
        try:
            SgRoot("", lang).root().find(kind=kind)
        except RuntimeError:
            return False
    return True


def build_lang_globs() -> dict[str, tuple[str, ...]]:
    claims: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for _name, aliases, globs, _mimetypes in get_all_lexers():
        if not (accepted := tuple(alias for alias in aliases if accepts_language(alias))):
            continue
        lang = min(enumerate(accepted), key=lambda item: (len(item[1]), item[0]))[1]
        for glob in globs:
            if match := GLOB_PATTERN.fullmatch(glob):
                claims[match[1].lower()].add((match[1], lang))

    owners: dict[str, str] = {}
    for ext, ext_claims in sorted(claims.items()):
        claimants = {lang for _spelling, lang in ext_claims}
        if len(claimants) == 1:
            owners[ext] = claimants.pop()
            continue
        lowercase_claimants = {lang for spelling, lang in ext_claims if spelling == ext}
        if len(lowercase_claimants) != 1:
            raise ValueError(f"case-fold collision for extension {ext!r}: {sorted(claimants)!r}")
        owners[ext] = lowercase_claimants.pop()

    return {
        lang: tuple(f"*.{ext}" for ext, owner in sorted(owners.items()) if owner == lang)
        for lang in sorted(set(owners.values()))
    }


def build_comment_types() -> frozenset[str]:
    by_lang = {
        lang: frozenset(kind for kind in COMMENT_KIND_SEEDS if defines_kind(lang, kind)) for lang in build_lang_globs()
    }
    if missing := sorted(lang for lang, kinds in by_lang.items() if not kinds and lang not in COMMENTLESS_LANGS):
        raise RuntimeError(f"generated languages define none of {COMMENT_KIND_SEEDS!r}: {missing!r}")
    return frozenset(kind for kinds in by_lang.values() for kind in kinds)


def build_doc_comment_kinds() -> frozenset[str]:
    by_seed = {seed: [lang for lang in build_lang_globs() if defines_kind(lang, seed)] for seed in DOC_KIND_SEEDS}
    if dead := sorted(seed for seed, langs in by_seed.items() if not langs):
        raise RuntimeError(f"doc-kind seeds matched no language: {dead!r}")
    return frozenset(by_seed)


def build_doc_siblings() -> dict[str, frozenset[str]]:
    table = {
        lang: frozenset(
            kind
            for kind, scope in DECL_KIND_SEEDS.items()
            if (scope is None or lang in scope) and defines_kind(lang, kind)
        )
        for lang in build_lang_globs()
    }
    if dead := sorted(kind for kind in DECL_KIND_SEEDS if not any(kind in kinds for kinds in table.values())):
        raise RuntimeError(f"decl-kind seeds matched no language: {dead!r}")
    return table


def render_langs() -> str:
    lang_globs = build_lang_globs()
    comment_types = build_comment_types()
    doc_comment_kinds = build_doc_comment_kinds()
    doc_siblings = build_doc_siblings()
    globs_body = "\n".join(f"    {lang!r}: {globs!r}," for lang, globs in lang_globs.items())
    comments_body = "\n".join(f"        {kind!r}," for kind in sorted(comment_types))
    doc_comments_body = "\n".join(f"        {kind!r}," for kind in sorted(doc_comment_kinds))
    doc_siblings_body = "\n".join(
        f"    {lang!r}: frozenset({tuple(sorted(kinds))!r})," if kinds else f"    {lang!r}: frozenset(),"
        for lang, kinds in doc_siblings.items()
    )
    return (
        f"# GENERATED by `{REGEN_COMMAND}`.\n"
        f"# Pygments {version('pygments')}; ast-grep-py {version('ast-grep-py')}.\n\n"
        "from __future__ import annotations\n\n"
        "LANG_GLOBS: dict[str, tuple[str, ...]] = {\n"
        f"{globs_body}\n"
        "}\n\n"
        "COMMENT_TYPES: frozenset[str] = frozenset(\n"
        "    {\n"
        f"{comments_body}\n"
        "    }\n"
        ")\n\n"
        "DOC_COMMENT_KINDS: frozenset[str] = frozenset(\n"
        "    {\n"
        f"{doc_comments_body}\n"
        "    }\n"
        ")\n\n"
        "DOC_SIBLINGS: dict[str, frozenset[str]] = {\n"
        f"{doc_siblings_body}\n"
        "}\n"
    )


def write_langs(path: Path) -> None:
    path.write_text(render_langs())


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        package = Path(self.root) / "captain_hook"
        write_langs(package / "langs.py")


if __name__ == "__main__":
    package = Path(__file__).parent / "captain_hook"
    write_langs(package / "langs.py")
