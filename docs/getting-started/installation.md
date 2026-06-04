# Installation

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) is required. The `uvx` runner ships with uv and is how every captain-hook command is invoked.

## Run without installing (default)

The fastest way to use captain-hook — no project dependency needed:

```bash
uvx --from cc-captain-hook captain-hook init
```

The PyPI distribution is `cc-captain-hook` while the command stays `captain-hook` — that's why the invocation is `uvx --from cc-captain-hook captain-hook`.

`uvx` fetches captain-hook into a throwaway environment, runs the CLI, and discards it. Every command works the same way — just prefix it with `uvx --from cc-captain-hook`:

```bash
uvx --from cc-captain-hook captain-hook test
uvx --from cc-captain-hook captain-hook generate-settings
```

Run from your project root and `--hooks` defaults to `.claude/hooks`. This is the headline path: you never add captain-hook to `pyproject.toml` and never manage a venv yourself.

## Add as a project dependency

Only if you want captain-hook pinned in your project's lockfile (e.g. to vendor it for offline CI):

```bash
uv add cc-captain-hook
```

Then the commands drop the `uvx` prefix:

```bash
captain-hook init
```

## Monorepo / local checkout

When captain-hook lives inside a larger repo (or you've cloned it for local development), point `uv run --project` at the local checkout:

```bash
uv run --project packages/captain-hook captain-hook test
```

This is the right shape for any consumer project that vendors captain-hook rather than pulling it from PyPI.

## Verify installation

```bash
uvx --from cc-captain-hook captain-hook test
```

This runs the inline tests on the scaffolded example hook. If you added captain-hook as a dependency, you can also check the import directly:

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

You don't need this exact layout — `captain-hook --hooks <any-dir> test` works on any directory of hooks. The default just lines up with what Claude Code looks for.

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
      - uses: astral-sh/setup-uv@v5
      - run: uvx --from cc-captain-hook captain-hook test
```

`setup-uv` installs uv (which provides Python 3.12+ and `uvx`), so there's no separate Python or install step. Drop `--json` in if a downstream reporter consumes the output. For the monorepo install mode, swap the run line for `uv run --project packages/captain-hook captain-hook test`.
