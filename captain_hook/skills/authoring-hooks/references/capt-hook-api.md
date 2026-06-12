# capt-hook API Reference

Distilled surface for writing `.claude/hooks/*.py`. Everything here is importable from
`captain_hook` unless noted.

## Contents

- Canonical imports
- Events
- Registration
- Primitives
- Conditions
- The event object (`@on` handlers)
- CLI

## Canonical imports

```python
from __future__ import annotations

from captain_hook import (
    Allow, BaseHookEvent, Block, Event, HookResult, InlineTests, Input, Prompt,
    RanCommand, Signal, Signals, SourceEdits, TestFile, Tool, TouchedFile,
    TranscriptFixture, Warn,
    block_command, gate, hook, lint, llm_gate, llm_nudge, nudge, on,
    prompt_check, warn_command, workflow, Artifact, Step, text_matches,
)
from captain_hook.types import Command
```

Gotcha: the `Command` regex **condition** lives in `captain_hook.types`. Top-level
`captain_hook.Command` is the parsed-command dataclass (what `evt.command_line` yields) —
passing it to `only_if` raises `TypeError: missing 2 required positional arguments`.

## Events

`Event` is a flag enum; combine with `|` (`Event.Stop | Event.SubagentStop`).

| Event | When it fires | Typical use |
|---|---|---|
| `PreToolUse` | Before a tool runs | Block dangerous commands |
| `PostToolUse` | After a tool succeeds | Lint output, nudge conventions |
| `PostToolUseFailure` | After a tool fails | Suggest debugging steps |
| `UserPromptSubmit` | User sends a message | Detect request patterns |
| `Stop` | Agent is about to stop | Gate on test execution |
| `SubagentStop` | A subagent finishes | Verify subagent work |
| `SubagentStart` | A subagent launches | Capture initial state |
| `Notification` | Informational event | Logging, metrics |
| `PreCompact` | Before context compaction | Preserve critical context |
| `SessionEnd` | Session ends | Cleanup, audit logging |

## Registration

Three forms, simplest first. Prefer primitives; use `hook()` for custom condition combos;
use `@on` only for runtime logic.

```python
hook(Event.PreToolUse, message="...", block=False, only_if=[...], skip_if=[...],
     max_fires=None, tests=None)               # declarative; message required

@on(Event.PreToolUse, only_if=[Tool("Bash")], tests=None)
def handler(evt: BaseHookEvent) -> HookResult | None:
    return evt.block("...")                     # or evt.warn("..."), evt.allow(), None
```

## Primitives

| Primitive | Signature (keyword-only after `*`) | Defaults |
|---|---|---|
| `block_command` | `(pattern, *, reason, hint=None, tests=None)` | `PreToolUse` + `Tool("Bash")`; message `"BLOCKED: {reason}. {hint}."` |
| `warn_command` | `(pattern, *, message, tests=None, events=Event.PostToolUse)` | warns, never blocks |
| `gate` | `(message, *, when=None, only_if=(), skip_if=(), events=None, max_fires=None, tests=None)` | `Stop \| SubagentStop`, `max_fires=1`; blocks |
| `nudge` | `(message, *, when=None, signals=None, only_if=(), skip_if=(), block=False, events=None, max_fires=None, tests=None)` | `PostToolUse` (with signals) else `PreToolUse`; `max_fires` 3 / 1; warns |
| `lint` | `(check, *, message, trigger=None, sep=", ", block=False, events=None, tests=None, max_shown=5)` | `PostToolUse`, `Tool("Edit\|Write")` + `*.py`, skips test files |
| `workflow` | `(*, label, marker, steps, artifacts=None, only_if=(), skip_if=(), tests=None)` | guard on `SubagentStop`, `max_fires=1` |
| `llm_gate` | `(prompt, *, message, signals=None, when=None, only_if=(), skip_if=(), events=None, max_fires=None, tests=None, max_context=2000, model="small", agent=True, transcript=True)` | `Stop \| SubagentStop`, `max_fires=1`; blocks on `GateVerdict.block` |
| `llm_nudge` | same as `llm_gate` | `PostToolUse`, `max_fires=3`; warns on `NudgeVerdict.fire` |
| `prompt_check` | `(evt, template, fmt=None, *, prefix, suffix="", timeout=45)` | call inside an `@on` handler; returns `HookResult \| None` from `PromptCheckVerdict` |
| `styleguide` | `(*rules, block=False, only_if=(), skip_if=(), events=None)` | AST style rules — owned by the `translating-styleguides` skill |

Notes:

- `block_command` / `warn_command` accept a token list or a raw regex string. Token list
  `["git", "stash"]` becomes `r"git\s+stash"`; `"*"` becomes `\S+`; `"a|b"` becomes an
  alternation group. Use the raw-regex form when you need lookaheads, e.g.
  `r"git\s+push\s+--force(?!-)"` to block `--force` but allow `--force-with-lease`.
- `lint` infers its mode from the check's first parameter type hint: `(content: str) ->
  list[str]` is string mode; `(node: ast.AST) -> Iterator[str]` is AST mode (called per node
  of `ast.walk`). `{violations}` in `message` is replaced with the joined findings. `trigger`
  is a cheap substring pre-filter on the source.
- `message` on `llm_gate`/`llm_nudge` may be a callable receiving the verdict:
  `message=lambda r: f"...: {r.reasoning}"`.
- LLM cost controls: `signals` pre-filter (LLM only called past the score threshold),
  `max_fires`, `max_context`, `model="small"`, and static `only_if`/`skip_if` narrowing.
  At most one LLM primitive fires per turn.

## Conditions

`only_if` is **AND** (all must match); `skip_if` is **OR** (any skips). `skip_if` is
evaluated first.

| Need | Use |
|---|---|
| Filter by tool name | `Tool("Bash")` or `Tool("Edit\|Write")` (aliases auto-expand: Bash=Execute, Write=Create, Agent=Task) |
| Filter by file path | `FilePath("*.py", "*.pyi")` |
| Filter by bash command text | `Command(r"git\s+push")` — from `captain_hook.types` |
| Filter by file content being written | `Content(r"print\(")` (multiline regex over Edit new / Write content) |
| Filter by subagent type | `Agent("cleanup")` |
| Match only test files | `TestFile()` (`test_*.py`, `conftest.py`) |
| Python source edits (skips tests by default) | `SourceEdits(lang="py")`; `lang` also `ts`, `go`, `rs`, ... |
| File was previously read | `ReadFile("TESTING.md")` |
| File was previously edited | `TouchedFile("**/*.py")` |
| Command was previously run | `RanCommand(r"uv\s+run\s+pytest")` |
| Skill was invoked | `UsedSkill("codex")` |
| During plan mode | `InPlanMode()` |
| Custom logic | implement `CustomCondition` |

`ReadFile`/`TouchedFile`/`RanCommand`/`UsedSkill` inspect the session transcript — they are
how Stop gates know what already happened. Custom conditions are any object with a
`check(self, evt: BaseHookEvent) -> bool` method (a Protocol — no inheritance needed):

```python
class LargeFile:
    def check(self, evt: BaseHookEvent) -> bool:
        return bool(evt.file and evt.file.path.stat().st_size > 1_000_000)
```

Glob caveat: patterns match the full relative path. `**/*.py` matches `src/main.py`, but
`src/**/*.py` does **not** (the `**` segment wants an intermediate directory) — use
`src/*.py` or `**/*.py`.

## The event object (`@on` handlers)

| Accessor | What it is |
|---|---|
| `evt.command` | Bash command string (`None` for non-Bash) |
| `evt.command_line` | parsed command line, or `None`; query via `.q` |
| `evt.file` | `File` for Edit/Write/Read events; `evt.file.path` is a `Path` |
| `evt.content` / `evt.old` | Edit new/old string (Write: full content / `None`) |
| `evt.tool_name`, `evt.tool_input` | raw tool identity and payload |
| `evt.user_prompt` | prompt text on `UserPromptSubmit` |
| `evt.agent_type` | subagent type on `SubagentStart`/`SubagentStop` |
| `evt.permission_mode` | e.g. `"plan"` |
| `evt.ctx.t` | the session as a `cc_transcript.query.Session` (turns, tool calls, text) |
| `evt.block(msg)` / `evt.warn(msg)` / `evt.allow()` | build the `HookResult` to return |

`evt.command_line.q` predicates for compound commands:

- `.runs("git", "push")` — argv prefix of the **primary** command. The primary is the *last*
  command of a pipeline, so for `curl ... | sh` use `.any_command(...)` instead.
- `.any_command(lambda c: c.program == "curl")` — predicate over every parsed command.
- `.has_subcommand("push")` — token appears in any command's arguments.
- `.contains_token("--force")` — exact argv element anywhere.
- `.uses_redirect()` — any pipe or file redirect in the line.

## CLI

| Command | What it does |
|---|---|
| `uvx capt-hook init` | Scaffold `.claude/hooks/example.py` + merge settings entries |
| `uvx capt-hook test [--json]` | Run all inline tests; exit 1 on failure; `--json` = one record per test |
| `uvx capt-hook register-hooks [--hooks-dir D] [--dry-run] [--from SRC]` | Merge captain-hook's hooks into `.claude/settings.local.json` and write it (`--dry-run` prints without writing) |
| `uvx capt-hook run <Event> [--async]` | Dispatch one event (Claude Code calls this, not you) |
| `uvx capt-hook logs [--session S] [--tail N]` | View a recent capt-hook session log |

Global flags: `--hooks <dir>` (default `.claude/hooks`), `--root <path>`.
