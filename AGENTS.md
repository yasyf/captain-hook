# captain-hook Development Guide

Declarative hook framework for Claude Code. Published to PyPI as `cc-captain-hook`; the CLI stays `captain-hook` (invoked as `uvx --from cc-captain-hook captain-hook`).

## Repository Structure

```
captain-hook/
├── captain_hook/     # The package — events, conditions, primitives, transcript, CLI
├── tests/            # Pytest suite (unit, integration, e2e install)
├── docs/             # MkDocs site (Material) — published to Read the Docs
│   └── examples/     # Self-contained example hooks (*.py) + their doc pages (*.md)
├── .github/          # CI (pytest + wheel smoke test) and PyPI release workflows
├── AGENTS.md         # This file — shared conventions
└── README.md         # Project overview
```

Docs API reference is generated from docstrings via mkdocstrings (`docs/gen_ref_pages.py`). Example hooks in `docs/examples/*.py` are embedded into doc pages via `pymdownx.snippets` and carry inline `tests = {...}` runnable with `captain-hook --hooks docs/examples test`.

## Python Style

Target Python 3.12+. Use `uv` for everything: `uv sync --extra dev`, `uv run pytest`, `uv build`.

**Docstrings on the public API only.** `captain_hook/types.py` and other user-facing surfaces carry Google-style docstrings — they render into the docs site. Internal helpers get none. No comments except TODOs, non-obvious workarounds, or disabled code.

**Functional over imperative.** Chain operations, use walrus `:=`, comprehensions over loops. No intermediate variables when a pipeline reads well.

**No underscore prefixes.** Use `__all__` for export control, not naming conventions.

**Match statements for type dispatch.** `if/elif` only for meaningful boolean flags.

**Minimal try/except.** Only the throwing line inside `try`. No broad exception handlers.

**No defensive coding, backwards-compat, or optional modeling.** No fallbacks or shims. Crash on unexpected errors. No sentinel values. No optional fields with fallback defaults; make fields required or split the model.

**Make invalid states unrepresentable.** `NewType` for branded primitives. Frozen models for immutable data. Required fields over optionals.

**Flat over nested.** Early returns, flat control flow. Nesting >3 levels is a smell.

**Type annotations everywhere.** `from __future__ import annotations`. Pyright runs in strict mode over `captain_hook/`.

## General Rules

**Minimal changes.** Stay within scope. Fix the issue, then stop.

**Match surrounding code.** Priority: (1) this file, (2) same file, (3) same module.

**Mechanical linting.** Do not run `ruff` manually for formatting/import-order fixes — CI and hooks handle it. Only fix issues requiring human judgment.

**Testing.** Suite lives in `tests/`, run with `uv run pytest`. Strict assertions. Mock external dependencies, not the code under test. NLP-dependent tests need `en_core_web_sm` (spaCy) and `oewn:2025` (wn) provisioned — see `.github/workflows/ci.yml`.

**Docs.** Any public API change must keep `uv run mkdocs build` green (`uv sync --group docs` first). New example hooks need both the `.py` in `docs/examples/` and a doc page wired into `mkdocs.yml` nav.

**Git.** Commits should be atomic and scoped. One logical change per commit.

**Releases.** Tag `v*` → `.github/workflows/release-pypi.yml` builds, publishes to PyPI (trusted publishing), and cuts a GitHub release. Version comes from the tag.
