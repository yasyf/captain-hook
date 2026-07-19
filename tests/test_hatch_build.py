from __future__ import annotations

import pytest

from hatch_build import (
    Crate,
    build_lang_globs,
    cargo_checksums,
    comment_kinds,
    dep_packages,
    doc_comment_kinds,
    lang_keys,
    parse_aliases,
    parse_extensions,
    parser_crates,
)

COMBINED_LIB = """\
impl_aliases! {
  Go => &["go", "golang"],
  Rust => &["rs", "rust"],
}

fn extensions(lang: SupportLang) -> &'static [&'static str] {
  use SupportLang::*;
  match lang {
    Go => &["go"],
    Rust => &["rs"],
  }
}
"""

COMBINED_LIB_MISSING_EXT = """\
impl_aliases! {
  Go => &["go", "golang"],
  Rust => &["rs", "rust"],
}

fn extensions(lang: SupportLang) -> &'static [&'static str] {
  use SupportLang::*;
  match lang {
    Go => &["go"],
  }
}
"""

LOCK = """\
[[package]]
name = "tree-sitter-rust"
version = "0.24.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "439e577dbe07423e"
dependencies = [
 "cc",
]

[[package]]
name = "tree-sitter-kotlin-sg"
version = "0.4.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c06ec43ae3c12165"

[[package]]
name = "some-path-dep"
version = "0.1.0"
"""

LIB = """\
impl_lang!(Bash, language_bash);
impl_lang_expando!(Kotlin, language_kotlin, 'µ');
impl_lang!(Tsx, language_tsx);
"""

PARSERS = """\
pub fn language_bash() -> TSLanguage {
  conditional_lang!(tree_sitter_bash, "tree-sitter-bash")
}
pub fn language_kotlin() -> TSLanguage {
  conditional_lang!(tree_sitter_kotlin, "tree-sitter-kotlin")
}
pub fn language_html() -> TSLanguage {
  conditional_lang!(tree_sitter_html, "tree-sitter-html")
}
pub fn language_tsx() -> TSLanguage {
  conditional_lang!(
    tree_sitter_typescript,
    "tree-sitter-typescript",
    LANGUAGE_TSX
  )
}
"""

CARGO_TOML = """\
[dependencies]
tree-sitter-bash = { version = "0.25.0", optional = true }
tree-sitter-kotlin = { version = "0.4.1", optional = true, package = "tree-sitter-kotlin-sg" }
tree-sitter-html = { version = "0.23.0", optional = true }
tree-sitter-typescript = { version = "0.23.2", optional = true }
"""

ALIASES = """\
impl_aliases! {
  Cpp => &["cc", "c++", "cpp", "cxx"],
  JavaScript => &["javascript", "js", "jsx"],
  Yaml => &["yaml", "yml"],
}
"""

EXTENSIONS = """\
fn extensions(lang: SupportLang) -> &'static [&'static str] {
  use SupportLang::*;
  match lang {
    Bash => &[
      "bash", "bats", "sh", "zsh",
    ],
    Python => &["py", "py3", "pyi", "bzl"],
    TypeScript => &["ts", "cts", "mts"],
  }
}
"""


def test_cargo_checksums_keeps_only_registry_crates() -> None:
    assert cargo_checksums(LOCK) == {
        "tree-sitter-rust": ("0.24.2", "439e577dbe07423e"),
        "tree-sitter-kotlin-sg": ("0.4.1", "c06ec43ae3c12165"),
    }


def test_parse_aliases() -> None:
    assert parse_aliases(ALIASES) == {
        "Cpp": ("cc", "c++", "cpp", "cxx"),
        "JavaScript": ("javascript", "js", "jsx"),
        "Yaml": ("yaml", "yml"),
    }


def test_parse_extensions_spans_multiline_arm() -> None:
    assert parse_extensions(EXTENSIONS) == {
        "Bash": ("bash", "bats", "sh", "zsh"),
        "Python": ("py", "py3", "pyi", "bzl"),
        "TypeScript": ("ts", "cts", "mts"),
    }


def test_parser_crates_resolves_html_field_and_rename() -> None:
    crates = parser_crates(
        PARSERS,
        LIB,
        CARGO_TOML,
        {
            "tree-sitter-bash": ("0.25.1", "aa"),
            "tree-sitter-kotlin-sg": ("0.4.1", "bb"),
            "tree-sitter-html": ("0.23.2", "cc"),
            "tree-sitter-typescript": ("0.23.2", "dd"),
        },
    )
    assert crates == {
        "Bash": Crate("tree-sitter-bash", "0.25.1", "aa", ""),
        "Kotlin": Crate("tree-sitter-kotlin-sg", "0.4.1", "bb", ""),
        "Html": Crate("tree-sitter-html", "0.23.2", "cc", ""),
        "Tsx": Crate("tree-sitter-typescript", "0.23.2", "dd", "tsx/"),
    }


def test_dep_packages_reads_rename() -> None:
    assert dep_packages(CARGO_TOML)["tree-sitter-kotlin"] == "tree-sitter-kotlin-sg"
    assert dep_packages(CARGO_TOML)["tree-sitter-bash"] == "tree-sitter-bash"


def test_lang_keys_prefers_shortest_alias_and_overrides() -> None:
    assert lang_keys(parse_aliases(ALIASES)) == {"Cpp": "cpp", "JavaScript": "js", "Yaml": "yaml"}


def test_lang_keys_rejects_override_that_is_not_an_alias() -> None:
    with pytest.raises(RuntimeError, match="not an upstream alias"):
        lang_keys({"Cpp": ("cc", "cxx")})


def test_comment_kinds_excludes_marker_children() -> None:
    nodes = [
        {"type": "line_comment", "named": True, "fields": {"doc": {"types": [{"type": "doc_comment", "named": True}]}}},
        {
            "type": "block_comment",
            "named": True,
            "fields": {"inner": {"types": [{"type": "inner_doc_comment_marker", "named": True}]}},
        },
        {"type": "inner_doc_comment_marker", "named": True, "fields": {}},
        {"type": "doc_comment", "named": True},
        {"type": "comment", "named": False},
        {"type": "identifier", "named": True},
    ]
    assert comment_kinds(nodes) == {"line_comment", "block_comment"}


def test_doc_comment_kinds_requires_marker_or_documentation() -> None:
    nodes = [
        {"type": "inner_doc_comment_marker", "named": True},
        {"type": "outer_doc_comment_marker", "named": True},
        {"type": "documentation_block_comment", "named": True},
        {"type": "doc_comment", "named": True},
        {"type": "heredoc_body", "named": True},
        {"type": "nowdoc_string", "named": True},
        {"type": "haddock", "named": True},
        {"type": "doctype", "named": True},
        {"type": "document", "named": True},
        {"type": "doc_marker", "named": False},
    ]
    assert doc_comment_kinds(nodes) == {
        "documentation_block_comment",
        "inner_doc_comment_marker",
        "outer_doc_comment_marker",
    }


def test_build_lang_globs_maps_keys_to_sorted_globs() -> None:
    assert build_lang_globs(COMBINED_LIB) == {"go": ("*.go",), "rs": ("*.rs",)}


def test_build_lang_globs_rejects_alias_variant_without_extensions() -> None:
    with pytest.raises(RuntimeError, match="no extensions parsed"):
        build_lang_globs(COMBINED_LIB_MISSING_EXT)


def test_parse_aliases_raises_without_block() -> None:
    with pytest.raises(RuntimeError, match="impl_aliases"):
        parse_aliases("// no macro block here")


def test_parse_extensions_raises_without_block() -> None:
    with pytest.raises(RuntimeError, match="fn extensions"):
        parse_extensions("// no extensions fn here")
