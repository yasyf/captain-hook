# captain-hook Development Guide

Declarative hook framework for Claude Code. Published to PyPI as `capt-hook`; the CLI is `capt-hook`, run as `uvx capt-hook`.

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

mkdocstrings generates the docs API reference from docstrings via `docs/gen_ref_pages.py`. Example hooks in `docs/examples/*.py` embed into doc pages via `pymdownx.snippets` and carry inline `tests = {...}` runnable with `capt-hook --hooks docs/examples test`.

## Python Style

Target Python 3.12+. Run `uv sync --extra dev`, `uv run pytest`, and `uv build`.

**Docstrings on the public API only.** `captain_hook/types.py` and other user-facing surfaces carry Google-style docstrings; they render into the docs site via mkdocstrings. Internal helpers get none. No comments except TODOs, non-obvious workarounds, or disabled code.

@STYLEGUIDE.md

## General Rules

**Minimal changes.** Stay within scope; fix the issue, then stop.

**Match surrounding code.** Follow the conventions of the file you're in, then the module.

**Code stewardship.** When you touch a file, fix nearby bugs, style violations, and broken tests; don't wave them off as pre-existing or out of scope. Trivial type-checker noise is the exception (see § Python Style).

**Mechanical linting.** CI and hooks handle formatting and import order. Leave `ruff` to them and fix only what needs human judgment.

**Testing.** The suite lives in `tests/`; run it with `uv run pytest`. Use strict assertions and mock external dependencies while leaving the code under test real. NLP-dependent tests need the `en_core_web_sm` spaCy model and the `oewn:2025` wn lexicon provisioned, as in `.github/workflows/ci.yml`.

**Docs.** Any public API change must keep `uv run mkdocs build` green; run `uv sync --group docs` first. New example hooks need both the `.py` in `docs/examples/` and a doc page wired into `mkdocs.yml` nav.

**Git.** Commits should be atomic and scoped. One logical change per commit.

**Releases.** Tagging `v*` triggers `.github/workflows/release-pypi.yml`, which builds, publishes to PyPI via trusted publishing, and cuts a GitHub release. The version comes from the tag.
