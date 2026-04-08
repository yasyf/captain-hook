# captain-hook

A declarative hook framework for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Build hooks that intercept Claude Code lifecycle events — tool use, stop, prompt submit — and produce actions (block, warn, allow) using composable conditions, signal scoring, and LLM-powered evaluation.

## Features

- **Declarative registration** — define hooks in one line with `hook()`, or use primitives like `nudge()`, `gate()`, `lint()`, and `block_command()`
- **Composable conditions** — filter hooks with `only_if` / `skip_if` using typed condition objects (`Tool`, `FilePath`, `Command`, `TouchedFile`, etc.)
- **Signal scoring** — score transcript text with regex and NLP patterns, with echo suppression and content-hash deduplication
- **LLM evaluation** — gate or nudge with LLM-powered verdicts via `llm_gate()` and `llm_nudge()`
- **Transcript querying** — rich, typed API for querying tool uses, commands, edits, and user messages
- **Inline testing** — test hooks declaratively with `Input(...)` / `Block(...)` / `Warn(...)` / `Allow()`
- **Strongly typed** — zero `Any` in the public API, full IDE autocomplete support

## Quick links

- [Installation](installation.md) — get started in 30 seconds
- [Quickstart](quickstart.md) — write your first hook in under 20 lines
- [Core Concepts](core-concepts.md) — events, conditions, registration, dispatch
- [Primitives Guide](primitives.md) — nudge, gate, lint, block_command, LLM hooks, workflow
- [Transcript & Signals Guide](transcript-signals.md) — querying transcripts and scoring text
- [Testing Guide](testing.md) — inline tests, mock_event, dispatch_test
- [API Reference](api-reference.md) — auto-generated from docstrings
