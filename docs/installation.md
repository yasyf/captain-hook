# Installation

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

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

Create a hooks directory for your project:

```
my-project/
├── .claude/
│   └── hooks/
│       ├── src/
│       │   ├── conf.py       # Settings (optional)
│       │   └── my_hooks.py   # Your hooks
│       └── bin/
│           └── hooks         # Entrypoint script
```

The entrypoint script runs the CLI:

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")/.."
python -m captain_hook run "$1" --hooks src --root "$(pwd)/../.."
```

Make it executable:

```bash
chmod +x .claude/hooks/bin/hooks
```

## Dependencies

`captain-hook` bundles these dependencies:

- **pydantic** / **pydantic-settings** — typed settings and data models
- **tree-sitter** / **tree-sitter-bash** — bash command parsing
- **spacy** — NLP-based signal matching and echo detection
- **wn** — WordNet synonym expansion for NLP signals
- **funcy** — functional utilities
- **orjsonl** — fast JSONL reading for transcripts
