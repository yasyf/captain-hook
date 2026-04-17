# Installation

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Run without installing

The fastest way to use captain-hook — no project dependency needed:

```bash
uvx captain-hook init
```

This uses [uv's tool runner](https://docs.astral.sh/uv/concepts/tools/) to install captain-hook in an isolated environment and run it directly.

## Install with uv

```bash
uv add captain-hook
```

## Install with pip

```bash
pip install captain-hook
```

## Development install

Clone the repository and install in editable mode with dev dependencies:

```bash
git clone https://github.com/your-org/captain-hook.git
cd captain-hook
uv sync --all-extras
```

## Verify installation

```bash
python -c "import captain_hook; print(captain_hook.__name__)"
```

## Project setup

Scaffold a hooks project automatically:

```bash
captain-hook init
```

This creates the hooks directory, entrypoint script, and example hook file. See the [CLI reference](../reference/cli.md#init) for details.

Or create the structure manually:

```
my-project/
├── .claude/
│   └── hooks/
│       ├── conf.py       # Settings (optional)
│       └── my_hooks.py   # Your hooks
```

## Dependencies

`captain-hook` bundles these dependencies:

- **pydantic** / **pydantic-settings** — typed settings and data models
- **tree-sitter** / **tree-sitter-bash** — bash command parsing
- **spacy** — NLP-based signal matching and echo detection
- **wn** — WordNet synonym expansion for NLP signals
- **funcy** — functional utilities
- **orjsonl** — fast JSONL reading for transcripts
