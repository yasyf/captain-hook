# Development

Working on captain-hook itself. For using captain-hook in your project, see [Installation](getting-started/installation.md).

## Setup

```bash
git clone https://github.com/yasyf/captain-hook
cd captain-hook
uv sync --extra dev
```

### NLP assets

Tests that exercise NLP signals need the spaCy English model and WordNet data:

```bash
uv run python -m spacy download en_core_web_sm
uv run python -c "import wn; wn.download('oewn:2025')"
```

This mirrors what CI provisions — see `.github/workflows/ci.yml`.

## Tests

```bash
uv run pytest
```

The example hooks in `docs/examples/` carry inline tests too:

```bash
uv run captain-hook --hooks docs/examples test
```

## Docs

The docs are MkDocs (Material), published to Read the Docs. API reference pages are generated from docstrings at build time by `docs/gen_ref_pages.py`.

```bash
uv sync --group docs
uv run mkdocs serve
```

## Build and smoke-test the wheel

```bash
uv build
uv venv --seed .wheel-smoke
uv pip install --python .wheel-smoke/bin/python dist/*.whl
.wheel-smoke/bin/captain-hook --help
```

## Using a local checkout

From a consumer project, point `uv run --project` at your clone:

```bash
uv run --project path/to/captain-hook captain-hook test
```

Or generate settings whose hook commands invoke your checkout instead of PyPI:

```bash
captain-hook generate-settings --from path/to/captain-hook
```

## Releasing

Tag a version and push — `.github/workflows/release-pypi.yml` builds the sdist and wheel, publishes to PyPI via trusted publishing, and cuts a GitHub release:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The package version is set from the tag at build time; the version in `pyproject.toml` is never bumped by hand.
