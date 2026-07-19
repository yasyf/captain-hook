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
    Allow, And, Agent, Ask, BaseHookEvent, Block, Event, FilePath, FromSubagent,
    HookResult, InlineTests, Input, Not, Or, Prompt, RanCommand, ReadFile, Rewrite, Runs,
    Signal, Signals, SkipPermissions, SourceEdits, TestFile, Tool, ToolInput, TouchedFile,
    TranscriptFixture, UsedSkill, Warn, WorkflowScript,
    approve, block_command, deny, gate, hook, lint, llm_approve, llm_gate, llm_nudge, nudge, on,
    prompt_check, rewrite_command, set_tool_input, warn_command, workflow, Artifact, Step, text_matches,
)
from captain_hook.types import Command as CommandCondition
```

Top-level `Command` is the parsed-shell dataclass (`cc_transcript.command.Command`) that
`evt.command_line` yields — you rarely import it directly. The regex **condition**
(`CommandCondition(r"git\s+push")`) lives at `captain_hook.types.Command`; import it under
the `CommandCondition` alias to keep the two apart.

## Events

`Event` is a flag enum; combine with `|` (`Event.Stop | Event.SubagentStop`).

<!-- gen:events -->
| Event | When it fires | Typical use |
|---|---|---|
| `PreToolUse` | Before a tool runs | Block dangerous commands |
| `PostToolUse` | After a tool succeeds | Lint output, nudge conventions |
| `PostToolUseFailure` | After a tool fails | Suggest debugging steps |
| `UserPromptSubmit` | User sends a message | Detect request patterns |
| `Stop` | Agent is about to stop | Gate on test execution |
| `SubagentStop` | A subagent finishes | Verify subagent work |
| `SubagentStart` | A subagent launches | Capture initial state |
| `PreCompact` | Before context compaction | Preserve critical context |
| `Notification` | Informational event | Logging, metrics |
| `SessionStart` | Session starts, resumes, clears, or compacts (`evt.source`) | Provision resources, prime state |
| `SessionEnd` | Session ends | Cleanup, audit logging |
| `PermissionRequest` | A permission dialog would be shown | Auto-answer dialogs (allow/deny/rewrite); no decision means the dialog shows |
<!-- /gen:events -->

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

<!-- gen:primitives -->
| Primitive | Signature (keyword-only after `*`) | Defaults |
|---|---|---|
| `block_command` | `(pattern, *, reason, hint=None, only_if=(), skip_if=(), tests=None)` | `PreToolUse` + `Tool("Bash")`; message `"BLOCKED: {reason}. {hint}."` |
| `warn_command` | `(pattern, *, message, only_if=(), skip_if=(), tests=None, events=Event.PostToolUse)` | warns, never blocks |
| `rewrite_command` | `(pattern=None, replace=None, *, only_if=(), skip_if=(), to=None, block=None, note=None, tests=None)` | `PreToolUse` + `Tool("Bash")`; a pattern with an ast-grep metavar (`cat $$$ARGS`) rewrites structurally via `ast_grep.rewrite`, otherwise `re.sub(pattern, replace, command)`; allows with the rewritten command |
| `set_tool_input` | `(field, value, *, tool, only_if=(), skip_if=(), note=None, tests=None)` | `PreToolUse` + `Tool(tool)`; fills a **missing** top-level input field with `value` and allows, never clobbering a present one |
| `gate` | `(message, *, when=None, signals=None, only_if=(), skip_if=(), events=None, max_fires=-1, tests=None, async_=False, skip_planning_agents=None)` | `Stop \| SubagentStop`; blocks, defaults to **unlimited** fires (keeps enforcing); `skip_if` is additive with an automatic `Waiting()` |
| `nudge` | `(message, *, when=None, signals=None, only_if=(), skip_if=(), block=False, events=None, max_fires=-1, tests=None, async_=False, skip_planning_agents=None)` | `PostToolUse` (with signals) else `PreToolUse`; default fires 3 / 1; `when` vetoes even with `signals`; warns |
| `lint` | `(check=None, *, pattern=None, message, lang='py', trigger=None, sep=', ', block=False, events=None, tests=None, max_shown=5)` | `PostToolUse`, `Tool("Edit\|Write")` + the `lang` globs, skips test files; `trigger` pre-filters string **and** ast checks |
| `workflow` | `(*, label, marker, steps, artifacts=None, post_complete=None, on_start=None, only_if=(), skip_if=(), tests=None)` | guard on `SubagentStop`, `max_fires=1` |
| `install_binary` | `(script, *, label=None, timeout=600, only_if=(), skip_if=(), tests=None)` | `SessionStart`, async; runs `script` via `/bin/sh` from the calling pack file's dir; always allows |
| `llm_gate` | `(prompt, *, message, response_model=GateVerdict, verdict=…, label=None, signals=None, when=None, contexts=(), only_if=(), skip_if=(), events=None, max_fires=-1, tests=None, max_context=2000, specialty='review', model='small', agent=True, transcript=True, diff=False)` | `Stop \| SubagentStop`; defaults to **unlimited** fires (keeps enforcing); blocks when `verdict(result)` — default `GateVerdict.block` |
| `llm_nudge` | `(prompt, *, message, response_model=NudgeVerdict, verdict=…, label=None, signals=None, when=None, contexts=(), only_if=(), skip_if=(), events=None, max_fires=-1, tests=None, async_=False, max_context=2000, specialty='review', model='small', agent=True, transcript=True, diff=False)` | `PostToolUse`, `max_fires=3`; warns when `verdict(result)` — default `NudgeVerdict.fire` |
| `prompt_check` | `(evt, template, fmt=None, *, prefix, suffix='', timeout=45, include_reasoning=True, diff=False, response_model=PromptCheckVerdict)` | call inside an `@on` handler; returns `HookResult \| None` from `PromptCheckVerdict` |
| `styleguide` | `(*rules, block=False, only_if=(), skip_if=(), events=None, max_shown=5)` | AST style rules — owned by the `translating-styleguides` skill |
| `approve` | `(label, *, events=Event.PreToolUse \| Event.PermissionRequest, only_if=(), skip_if=(), tests=None)` | `PreToolUse \| PermissionRequest`; pre-authorizes matching tools before the prompt and answers matching dialogs with allow; **no fire cap**. Unconditioned == a permanent `--dangerously-skip-permissions`; always scope with conditions |
| `deny` | `(reason, *, events=Event.PreToolUse \| Event.PermissionRequest, only_if=(), skip_if=(), tests=None)` | `PreToolUse \| PermissionRequest`; blocks matching tools before the prompt and answers matching dialogs with deny, `reason` shown to the user; no fire cap. Unconditioned bricks every tool |
| `llm_approve` | `(label, *, events=Event.PermissionRequest, rubric=None, only_if=(), skip_if=(), model='small', tests=None)` | `PermissionRequest`; LLM safety judge seeded from `claude auto-mode defaults` (+ your `rubric`); a safe verdict allows, an unsafe verdict or LLM failure returns `None` so the dialog shows, never an auto-deny. One LLM round-trip per matching ask |
<!-- /gen:primitives -->

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
  Hooks using `Introduced` or `BeforeEdit` over Writes must set `events=Event.PreToolUse`:
  at `PostToolUse` a Write's pre-image is unknowable (disk already holds the new text),
  so the block is omitted and `required` contexts skip the call.
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
| `Allow(explicit=True)` | result is an actual `allow`; `None` fails, so a `PermissionRequest` hook must have answered the dialog itself | — |
| `Block(pattern=...)` | result is `block` | regex over the block message |
| `Warn(pattern=...)` | result is `warn` | regex over the warning message |
| `Rewrite(pattern=...)` | result is `rewrite` | **substring** of the rewritten `updated_input["command"]` |
| `Ask()` | result is `None`; for `PermissionRequest` the dialog shows, and a `warn` fails | — |

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

<!-- gen:conditions -->
| Need | Use |
|---|---|
| Filter by tool name | `Tool("Bash")` or `Tool("Edit", "Write")` — exact names (not regex), aliases auto-expand (Bash=Execute, Write=Create, Agent=Task), MCP suffixes match |
| Filter by file path | `FilePath("*.py", "*.pyi")` |
| Filter by bash command text | `CommandCondition(r"git\s+push")` (`captain_hook.types.Command`) — regex over the raw line and each parsed command |
| Filter by file content being written | `Content(r"print\(")` (multiline regex over Edit new / Write content) |
| Filter by raw tool-input fields | `ToolInput(model=r"(?i)\bhaiku\b")` (kwargs AND across fields; scalar values coerced to text) |
| Filter by a Workflow script | `WorkflowScript(model="haiku")` — any `agent()` opt as a kwarg (`effort=`, `agentType=`, …), all AND |
| Match edit content by code shape (ast-grep) | `Pattern("os.system($CMD)")` — structural, ignores matches inside strings/comments; `lang` inferred from the edited file's extension |
| Filter by subagent type | `Agent("cleanup")` or `Agent("Explore", "claude-code-guide")` |
| Event comes from a subagent/teammate | `FromSubagent()` — the payload carries an `agent_id`; matches the ask's *origin*, where `Agent` matches its *type* |
| Session launched with bypass available | `SkipPermissions()` — walks to the nearest `claude` ancestor process and matches `--dangerously-skip-permissions` **or** `--allow-dangerously-skip-permissions`; availability counts as consent, whatever the active `permission_mode` |
| Skill was invoked | `UsedSkill("codex")` — bare name also matches `plugin:name` |
| File was previously read | `ReadFile("TESTING.md")` — fnmatch globs; anchor dirs with `**/` |
| Match only test files | `TestFile()` (`**/test_*.py`, `**/*_test.py`, `**/conftest.py`, `**/tests/**/*.py`, `**/*_test.go`, `**/*.test.*`, `**/*.spec.*`) |
| Python source edits (skips tests by default, in-repo only) | `SourceEdits(lang="py")`; `lang` also `ts`, `go`, `rs`, ...; `project_only=False` to also match out-of-repo files |
| File was previously edited | `TouchedFile("**/*.py")` |
| Command was previously run | `RanCommand("uv", "run", "pytest")` — argv-prefix tokens, wrapper-transparent (`sudo`/`env`/`timeout` stripped) but launcher-literal (`uv run pytest` ≠ `pytest`; list each spelling as its own entry) |
| Bash argv prefix (structural, no false positives) | `Runs("git", "stash")` — matches `git stash [...]`, not `echo git stash` |
| During plan mode | `InPlanMode()` |
| Session is parked on background work | `Waiting()` — background shells/subagents/workflows in flight, or an undelivered task notification; typically `skip_if=[Waiting()]` on Stop gates |
| Combine across types | `Or(...)`, `And(...)`, `Not(...)` |
| Custom logic | implement `CustomCondition` |
<!-- /gen:conditions -->

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
| `evt.context(msg)` | `evt.warn` minus the `PreToolUse` auto-approve rider — inject `additionalContext` without approving the call |
| `evt.rewrite_command(new_command, *, note=None)` | **PreToolUse and PermissionRequest** — rewrite a Bash command in place (keeps the rest of the tool input), allowing it; `note` surfaces as `additionalContext` (dropped on `PermissionRequest`) |
| `evt.rewrite(updated_input, *, note=None)` | **PreToolUse and PermissionRequest** — replace the tool input wholesale with `updated_input` (same tool schema), allowing it |

`evt.command_line.q` predicates for compound commands:

- `.runs("git", "push")` — argv prefix of the **primary** command. The primary is the *last*
  command of a pipeline, so for `curl ... | sh` use `.any_command(...)` instead.
- `.any_command(lambda c: c.program == "curl")` — predicate over every parsed command.
- `.has_subcommand("push")` — token appears in any command's arguments.
- `.contains_token("--force")` — exact argv element anywhere.
- `.uses_redirect()` — any pipe or file redirect in the line.

Structural (ast-grep) matching over a command line goes through the `captain_hook.ast_grep`
free functions with the raw text and the `"bash"` language:

```python
from captain_hook import ast_grep

ast_grep.matches(cl.raw, "bash", "cat $$$ARGS")                 # bool
ast_grep.rewrite(cl.raw, "bash", "cat $$$ARGS", "bat $$$ARGS")  # rewritten str (unchanged when no match)
ast_grep.capture(cl.raw, "bash", "sed -n $R $F")                # {"R": ..., "F": ...} | None
```

## CLI

| Command | What it does |
|---|---|
| `uvx --isolated capt-hook init` | Scaffold `.claude/hooks/example.py` + register the captain-hook plugin |
| `uvx --isolated capt-hook test [--json]` | Run all inline tests; exit 1 on failure; `--json` = one record per test |
| `uvx --isolated capt-hook run <Event> [--async]` | Dispatch one event (Claude Code calls this, not you) |
| `uvx --isolated capt-hook logs [--session S] [--tail N]` | View a recent capt-hook session log |

Global flags: `--hooks <dir>` (default `.claude/hooks`), `--root <path>`.
