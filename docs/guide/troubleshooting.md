# Troubleshooting

Common errors and how to diagnose them.

## "My hook didn't fire"

Four things to check, in order:

1. Run `captain-hook test --hooks <dir>` with the inline tests in place. If the test fails, the hook's conditions aren't matching the input you expect — fix the condition before debugging dispatch.
2. Confirm the `--hooks` argument points at the directory that actually contains your hook file. Captain-hook loads `.py` files under that root and nothing else.
3. Inspect the `events=` argument. A primitive defaults to one event (`block_command` → `PreToolUse`, `nudge` → `PostToolUse`); if you want a different event, pass it explicitly.
4. View the recent dispatch logs with `captain-hook logs`. By default it prints the most recent session's per-event log file, written under the cache log dir (default `~/.cache/captain-hook/logs`, [see Configuration](configuration.md#log-directory)). Pass `--tail N` to limit lines, or `--session <id-or-transcript-path>` to target a specific session. If your hook never appears in the log, dispatch never reached it — recheck steps 1–3.

## Settings not picked up

Captain-hook reads settings from environment variables with the `HOOKS_` prefix. A `HooksSettings` subclass only auto-loads if you build it through `build_settings(YourSettings)` or expose it from your `conf.py` so the dispatch CLI can discover it.

- Run `printenv | rg HOOKS_` to confirm the variable is set.
- If you changed the prefix via `model_config = SettingsConfigDict(env_prefix="MYAPP_")`, double-check that nothing else still expects `HOOKS_`.
- See [Configuration](configuration.md) for the full settings surface.

## `RuntimeError: spaCy model is not installed`

NLP signals refuse to auto-download the spaCy model at hook execution time (it's a ~100MB silent fetch). Install it once, explicitly:

```bash
python -m spacy download en_core_web_sm
```

Or, if you want the model cached at the location captain-hook expects:

```bash
python -c "from captain_hook.util.model_cache import ensure_spacy_model; ensure_spacy_model()"
```

## Hook fired twice

If a primitive emits the same nudge / block twice for one event:

- Inspect `events=` on every registration. A hook registered with `Event.Stop | Event.SubagentStop` fires on both — confirm that's what you want.
- For `nudge`, add `max_fires=N` to cap how many times it can emit in a session.
- For signal-driven nudges, the same trigger phrase may live in multiple `Signal` patterns. Run `captain-hook test --verbose` and check which pattern matched.

## "Inline test failed but I can't tell why"

```bash
captain-hook test --hooks .claude/hooks --verbose
```

Verbose mode prints the event payload it built, every condition's result, and the resulting `Action`. If the test still looks correct, dump the `Input` you're constructing — the most common mistake is leaving a field off (e.g. `file=` for an Edit test) so the hook's condition can't match.

## "How do I see what events look like?"

Drop an `audit()` into your hooks directory for one session:

```python
from captain_hook import Event, audit

audit(Event.PreToolUse | Event.PostToolUse | Event.Stop)
```

It writes one JSONL line per event under the log directory ([see Configuration](configuration.md#log-directory)). Tail the file while the agent runs to see the exact `tool_input`, `tool_name`, and metadata your hooks will receive.

## Diagnostic commands

```bash
captain-hook test --hooks <dir>                   # Run inline tests
captain-hook test --hooks <dir> --verbose         # Inline tests with full event traces
captain-hook test --hooks <dir> --json            # Machine-readable output for CI
captain-hook run <Event> --hooks <dir> < event.json   # Replay an event JSON payload
captain-hook generate-settings --hooks <dir>      # Emit the .claude/settings.local.json fragment
```
