# Installation

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) is recommended but not required.

## Three install modes

### 1. Install (default)

For most projects: add captain-hook as a dependency.

```bash
pip install captain-hook
# or
uv add captain-hook
```

Then scaffold:

```bash
captain-hook init
```

### 2. Run without installing (uvx)

For one-shot scaffolding or trying things out, no project dependency:

```bash
uvx captain-hook init
```

`uvx` builds a throwaway venv, runs the CLI, and discards it. Good for repos where you don't want captain-hook in `pyproject.toml` yet.

### 3. Monorepo / local checkout

When captain-hook lives inside a larger repo (or you've cloned it for local development), point `uv run --project` at the local checkout:

```bash
uv run --project packages/captain-hook captain-hook test --hooks .claude/hooks
```

This is the right shape for any consumer project that vendors captain-hook rather than installs it from PyPI.

## Verify installation

```bash
python -c "import captain_hook; print(captain_hook.__name__)"
```

## Project layout

`captain-hook init` produces:

```
my-project/
├── .claude/
│   ├── hooks/
│   │   ├── conf.py       # Settings (optional)
│   │   └── example.py    # Starter hook
│   └── settings.local.json   # Captain-hook wires itself in here
```

You don't need this exact layout — `captain-hook test --hooks <any-dir>` works on any directory of hooks. The default just lines up with what Claude Code looks for.

## Dependencies

`captain-hook` bundles:

- **pydantic** / **pydantic-settings** — typed settings and data models
- **tree-sitter** / **tree-sitter-bash** — bash command parsing for `CommandLine`
- **spacy** — NLP-based signal matching and echo detection
- **wn** — WordNet synonym expansion for NLP signals
- **funcy** — functional utilities
- **orjsonl** — fast JSONL reading for transcripts

The spaCy English model is **not** auto-downloaded — see [Troubleshooting](../guide/troubleshooting.md#runtimeerror-spacy-model-is-not-installed).

## CI integration

Run inline hook tests in CI so scaffold edits cannot ship broken expectations. `captain-hook test --json` emits one JSON object per test (`id`, `status`, `expected`, `reason`) and exits non-zero on the first failure.

```yaml
# .github/workflows/hooks.yml
name: hooks

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install captain-hook
      - run: captain-hook test --hooks .claude/hooks
```

Drop `--json` in if a downstream reporter consumes the output. For the monorepo install mode, swap the install/run lines for `uv run --project packages/captain-hook captain-hook test --hooks .claude/hooks`.
