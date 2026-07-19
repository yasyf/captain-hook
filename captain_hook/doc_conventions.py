"""Hand-maintained documentation-comment convention data.

Which declaration kinds take documentation comments is ecosystem convention, not a grammar fact,
and it is measurably non-derivable: the best structural rule over the grammars — subtypes of
declaration-named supertypes — reproduces 0 of the 17 populated languages exactly, 9 of those 17
grammars have no usable declaration supertype at all, and exact reproduction would need a 118-entry
overlay on top of this 110-entry table. So unlike its generated siblings in
:mod:`captain_hook.langs`, this table is spelled out by hand.

To update on an ast-grep band bump: a new language gets a row here, keyed by the same short id as
:data:`~captain_hook.langs.LANG_GLOBS` — an empty ``frozenset()`` when its ecosystem has no
doc-adjacency convention; the ``set(DOC_SIBLINGS) == set(LANG_GLOBS)`` invariant in
``tests/test_langs.py`` fails until the row exists.
"""

from __future__ import annotations

DOC_SIBLINGS: dict[str, frozenset[str]] = {
    "bash": frozenset(),
    "c": frozenset(("field_declaration", "function_definition", "type_definition")),
    "cpp": frozenset(("field_declaration", "function_definition", "namespace_definition", "type_definition")),
    "cs": frozenset(
        (
            "class_declaration",
            "constructor_declaration",
            "enum_declaration",
            "field_declaration",
            "interface_declaration",
            "method_declaration",
            "namespace_declaration",
            "property_declaration",
            "record_declaration",
            "struct_declaration",
            "type_declaration",
        )
    ),
    "css": frozenset(),
    "dart": frozenset(("class_declaration", "enum_declaration", "function_declaration", "method_declaration")),
    "ex": frozenset(),
    "go": frozenset(
        (
            "const_declaration",
            "const_spec",
            "field_declaration",
            "function_declaration",
            "import_declaration",
            "method_declaration",
            "method_elem",
            "package_clause",
            "type_declaration",
            "var_declaration",
            "var_spec",
        )
    ),
    "hcl": frozenset(),
    "hs": frozenset(),
    "html": frozenset(),
    "java": frozenset(
        (
            "annotation_type_declaration",
            "class_declaration",
            "constructor_declaration",
            "enum_declaration",
            "field_declaration",
            "import_declaration",
            "interface_declaration",
            "method_declaration",
            "record_declaration",
        )
    ),
    "js": frozenset(("class_declaration", "export_statement", "function_declaration", "method_definition")),
    "json": frozenset(),
    "kotlin": frozenset(("class_declaration", "function_declaration", "object_declaration", "property_declaration")),
    "lua": frozenset(("function_declaration",)),
    "md": frozenset(),
    "nix": frozenset(),
    "php": frozenset(
        (
            "class_declaration",
            "const_declaration",
            "enum_declaration",
            "function_definition",
            "interface_declaration",
            "method_declaration",
            "namespace_definition",
            "property_declaration",
        )
    ),
    "py": frozenset(),
    "rb": frozenset(("class", "method", "module", "singleton_method")),
    "rs": frozenset(
        (
            "const_item",
            "enum_item",
            "field_declaration",
            "function_item",
            "impl_item",
            "mod_item",
            "static_item",
            "struct_item",
            "trait_item",
            "type_item",
        )
    ),
    "scala": frozenset(
        (
            "class_definition",
            "function_declaration",
            "function_definition",
            "import_declaration",
            "object_definition",
            "package_clause",
            "trait_definition",
            "type_definition",
            "val_definition",
            "var_declaration",
        )
    ),
    "solidity": frozenset(
        (
            "contract_declaration",
            "enum_declaration",
            "function_definition",
            "interface_declaration",
            "state_variable_declaration",
            "struct_declaration",
        )
    ),
    "swift": frozenset(
        (
            "class_declaration",
            "function_declaration",
            "import_declaration",
            "property_declaration",
            "protocol_declaration",
        )
    ),
    "ts": frozenset(
        (
            "abstract_class_declaration",
            "class_declaration",
            "enum_declaration",
            "export_statement",
            "function_declaration",
            "interface_declaration",
            "method_definition",
            "type_alias_declaration",
        )
    ),
    "tsx": frozenset(
        (
            "abstract_class_declaration",
            "class_declaration",
            "enum_declaration",
            "export_statement",
            "function_declaration",
            "interface_declaration",
            "method_definition",
            "type_alias_declaration",
        )
    ),
    "yaml": frozenset(),
}
"""Concrete declaration kinds a comment run may document, by language."""
