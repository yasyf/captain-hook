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
    Allow, And, Agent, BaseHookEvent, Block, Command, Event, FilePath, HookResult, InlineTests,
    Input, Not, Or, Prompt, RanCommand, ReadFile, Rewrite, Runs, Signal, Signals, SourceEdits,
    TestFile, Tool, ToolInput, TouchedFile, TranscriptFixture, UsedSkill, Warn, WorkflowScript,
    block_command, gate, hook, lint, llm_gate, llm_nudge, nudge, on,
    prompt_check, rewrite_command, set_tool_input, warn_command, workflow, Artifact, Step, text_matches,
)
```

`Command` is the regex **condition** (`Command(r"git\s+push")`). The parsed-shell dataclass
that `evt.command_line` yields is `ParsedCommand` (`captain_hook.command.ParsedCommand`) — you
rarely import it directly.

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
| `SessionStart` | Session starts, resumes, clears, or compacts (`evt.source`) | Provision resources, prime state |
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
| `block_command` | `(pattern, *, reason, hint=None, only_if=(), skip_if=(), tests=None)` | `PreToolUse` + `Tool("Bash")`; message `"BLOCKED: {reason}. {hint}."` |
| `warn_command` | `(pattern, *, message, only_if=(), skip_if=(), tests=None, events=Event.PostToolUse)` | warns, never blocks |
| `rewrite_command` | `(pattern, replace, *, only_if=(), skip_if=(), note=None, tests=None)` | `PreToolUse` + `Tool("Bash")`; `re.sub(pattern, replace, command)` then allows with the rewritten command |
| `set_tool_input` | `(field, value, *, tool, only_if=(), skip_if=(), note=None, tests=None)` | `PreToolUse` + `Tool(tool)`; fills a **missing** top-level input field with `value` and allows, never clobbering a present one |
| `gate` | `(message, *, when=None, signals=None, only_if=(), skip_if=(), events=None, max_fires=…, tests=None)` | `Stop \| SubagentStop`; blocks, defaults to **unlimited** fires (keeps enforcing); `skip_if` is additive with an automatic `Waiting()` |
| `nudge` | `(message, *, when=None, signals=None, only_if=(), skip_if=(), block=False, events=None, max_fires=…, tests=None)` | `PostToolUse` (with signals) else `PreToolUse`; default fires 3 / 1; `when` vetoes even with `signals`; warns |
| `lint` | `(check, *, message, lang="py", trigger=None, sep=", ", block=False, events=None, tests=None, max_shown=5)` | `PostToolUse`, `Tool("Edit\|Write")` + the `lang` globs, skips test files; `trigger` pre-filters string **and** ast checks |
| `workflow` | `(*, label, marker, steps, artifacts=None, only_if=(), skip_if=(), tests=None)` | guard on `SubagentStop`, `max_fires=1` |
| `llm_gate` | `(prompt, *, message, response_model=GateVerdict, verdict=…, signals=None, when=None, contexts=(), only_if=(), skip_if=(), events=None, max_fires=…, tests=None, max_context=2000, specialty="review", model="small", agent=True, transcript=True, diff=False)` | `Stop \| SubagentStop`; defaults to **unlimited** fires (keeps enforcing); blocks when `verdict(result)` — default `GateVerdict.block` |
| `llm_nudge` | same as `llm_gate` (default `response_model=NudgeVerdict`), plus `async_=False` | `PostToolUse`, `max_fires=3`; warns when `verdict(result)` — default `NudgeVerdict.fire` |
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
- `contexts=` on `llm_gate`/`llm_nudge` attaches declarative evidence blocks — any
  `PromptContext` (importable from `captain_hook`), each rendered as a named XML block in
  array order. Built-ins: `BeforeEdit`/`AfterEdit` (ambient defaults on every LLM primitive:
  the pending edit's before/after text, empty off edit events) and
  `Introduced(kind=... | pattern=...)` — AST constructs the pending edit newly introduces,
  diffed between the pre-image (`evt.replaced`) and `evt.content`; `kind=COMMENT_TYPES`
  extracts comments across languages; subclass and override `keep(text)` to filter. A
  `required` context (the `Introduced` default) with no evidence skips the LLM call
  entirely, consuming no fire — the extraction is the cheap trigger, the LLM the confirmer.
  Passing your own `contexts` with no `signals`/`when` suppresses the implicit transcript
  `<context>` block.
- LLM cost controls: `signals` pre-filter (LLM only called past the score threshold),
  a `required` context gate, `max_fires`, `max_context`, `model="small"`, and static
  `only_if`/`skip_if` narrowing. At most one LLM primitive fires per turn.

## Inline test expectations

`tests={Input(...): <expectation>}` maps an input to its expected outcome:

| Expectation | Passes when | `pattern` |
|---|---|---|
| `Allow()` | result is `None` or `allow` | — |
| `Block(pattern=...)` | result is `block` | regex over the block message |
| `Warn(pattern=...)` | result is `warn` | regex over the warning message |
| `Rewrite(pattern=...)` | result is `rewrite` | **substring** of the rewritten `updated_input["command"]` |

`Rewrite`'s `pattern` is a substring (not a regex), so an absolute-path prefix in the
rewritten command (e.g. `/abs/bin/ccx read x --full`) still matches `Rewrite(pattern="ccx
read x --full")`.

```python
rewrite_command(r"^cat\s+(\S+)$", r"ccx read \1 --full", note="ran ccx", tests={
    Input(command="cat foo.py"): Rewrite(pattern="ccx read foo.py --full"),
    Input(command="ls -la"): Allow(),
})
```

## Conditions

`only_if` is **AND** (all must match); `skip_if` is **OR** (any skips). `skip_if` is
evaluated first.

| Need | Use |
|---|---|
| Filter by tool name | `Tool("Bash")` or `Tool("Edit", "Write")` — exact names (not regex), aliases auto-expand (Bash=Execute, Write=Create, Agent=Task), MCP suffixes match |
| Filter by file path | `FilePath("*.py", "*.pyi")` |
| Filter by bash command text | `Command(r"git\s+push")` — regex over the raw line and each parsed command |
| Bash argv prefix (structural, no false positives) | `Runs("git", "stash")` — matches `git stash [...]`, not `echo git stash` |
| Filter by file content being written | `Content(r"print\(")` (multiline regex over Edit new / Write content) |
| Filter by raw tool-input fields | `ToolInput(model=r"(?i)\bhaiku\b")` (kwargs AND across fields; scalar values coerced to text) |
| Filter by a Workflow script | `WorkflowScript(model="haiku")` — any `agent()` opt as a kwarg (`effort=`, `agentType=`, …), all AND |
| Filter by subagent type | `Agent("cleanup")` or `Agent("Explore", "claude-code-guide")` |
| Match only test files | `TestFile()` (`test_*.py`, `conftest.py`, any `.py` under `tests/`) |
| Python source edits (skips tests by default) | `SourceEdits(lang="py")`; `lang` also `ts`, `go`, `rs`, ... |
| File was previously read | `ReadFile("TESTING.md")` — fnmatch globs; anchor dirs with `**/` |
| File was previously edited | `TouchedFile("**/*.py")` |
| Command was previously run | `RanCommand(r"uv\s+run\s+pytest")` |
| Skill was invoked | `UsedSkill("codex")` — bare name also matches `plugin:name` |
| During plan mode | `InPlanMode()` |
| Combine across types | `Or(...)`, `And(...)`, `Not(...)` |
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
| `evt.rewrite_command(new_command, *, note=None)` | **PreToolUse only** — rewrite a Bash command in place (keeps the rest of the tool input), allowing it; `note` surfaces as `additionalContext` |
| `evt.rewrite(updated_input, *, note=None)` | **PreToolUse only** — replace the tool input wholesale with `updated_input` (same tool schema), allowing it |

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
| `uvx capt-hook register-hooks [--hooks-dir D] [--dry-run] [--from SRC]` | Merge captain-hook's hooks into `.claude/settings.json` and write it (`--dry-run` prints without writing) |
| `uvx capt-hook run <Event> [--async]` | Dispatch one event (Claude Code calls this, not you) |
| `uvx capt-hook logs [--session S] [--tail N]` | View a recent capt-hook session log |

Global flags: `--hooks <dir>` (default `.claude/hooks`), `--root <path>`.
