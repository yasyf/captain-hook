# DX Audit — captain-hook v2

Cold audit of the entire public API surface. Every symbol in `__all__` (140 exports) was read, and 6 common hook patterns were written using only the public API.

## Methodology

1. Read every export in `captain_hook.__all__` (140 symbols)
2. Attempted to write these patterns from scratch:
   - Block a dangerous command (`rm -rf /`)
   - Lint Python for `print()` usage (AST mode)
   - Signal-driven nudge for retry language
   - LLM-gated review for test quality
   - Multi-step workflow with artifact validation
   - Custom condition with `@app.on()` handler
3. Documented every friction point encountered
4. Evaluated cross-cutting concerns: import ergonomics, CLI, testing, error messages

---

## Friction Points

### FP-1: `Prompt` is an alias for `PromptMessage` — confusing dual identity

**Area:** Naming

`prompt.py` defines `PromptMessage` as the real dataclass, then aliases it: `Prompt = PromptMessage`. Both are exported in `__all__`. A developer seeing `Prompt` in examples and `PromptMessage` in type signatures will be confused about whether they're different types.

**Resolution:** Design decision — keep both. `Prompt` is the short form for hook authors (`Prompt().system(...).context(...).ask(...)`), `PromptMessage` is the type-annotation-friendly name. Document that they are the same class. The `Prompt` alias exists for brevity at call sites; `PromptMessage` reads better in type hints. No change needed, but docstrings on both should make the alias relationship explicit.

---

### FP-2: `hook()` module-level function vs `app.hook()` method — easy to confuse

**Area:** Naming / Registration

The public API exports both a module-level `hook()` function and `HookApp.hook()` method. They have *different signatures*: `app.hook()` requires `message: str` (positional keyword), while the module-level `hook()` acts as a dual-purpose registration function — if `message=` is provided, it registers declaratively (returning `None`); if omitted, it returns a decorator for handler registration.

When writing a hook, a new user must decide between:
- `hook(Event.PreToolUse, message="blocked", block=True)` (declarative via module function)
- `app.hook(Event.PreToolUse, message="blocked", block=True)` (declarative via app method)
- `@app.on(Event.PreToolUse)` (handler decorator via app method)
- `hook(Event.PreToolUse)(my_handler)` (handler decorator via module function)

**Resolution:** Design decision — the split exists intentionally. The module-level `hook()` enables import-time registration (hooks auto-register when the module is imported during `discover_hooks`). The `app.hook()`/`app.on()` methods provide explicit app-scoped registration for tests and programmatic use. The `hook()` dual behavior (declarative when `message=` present, decorator when absent) matches the v1 API and avoids forcing users to learn two separate import-time functions. Document the three main patterns clearly in the quickstart:
1. `hook(events, message=..., block=True)` — declarative (most hooks)
2. `@app.on(events)` — handler (complex logic)
3. Primitives (`nudge()`, `gate()`, `lint()`, etc.) — convenience wrappers

---

### FP-3: 140 exports in `__all__` — overwhelming flat namespace

**Area:** Import ergonomics

`captain_hook.__init__.py` exports 140 symbols in `__all__`. For a new developer running `from captain_hook import `, IDE autocomplete returns an undifferentiated wall of names mixing core framework types (`HookApp`, `Event`), internal plumbing (`text_hash`, `fired_this_turn`, `record_fire`), transcript internals (`parse_content_block`, `parse_tool_input`), and testing utilities (`mock_event`, `dispatch_test`).

A hook author writing their first hook needs ~10 of these 140 symbols. The rest are noise.

**Resolution:** This is a deliberate design choice — flat imports are idiomatic Python and work well with IDE autocomplete once you know the names. The alternative (sub-package imports like `from captain_hook.testing import mock_event`) already works and is the recommended pattern for advanced use. The fix is documentation, not restructuring:
- The quickstart should show the 5-10 imports a typical hook needs
- Group the 140 exports by purpose in the API reference (Core, Conditions, Primitives, Testing, Transcript, Signals, Internals)
- Consider adding a `captain_hook.prelude` module with only the ~20 most common imports for quick starts

---

### FP-4: `ctx.state[Model]` / `ctx.s[Model]` — class-keyed state is non-obvious

**Area:** State management

Accessing session-persisted state uses `evt.ctx.s[PrimitiveState].get()`, returning `PrimitiveState | None`. This pattern is powerful (type-safe, no string keys) but unfamiliar. A developer must understand:
1. `ctx.s` is a `SessionStore` (alias for `ctx.state` and `ctx.session`)
2. `ctx.s[MyModel]` returns a `SessionSlot[MyModel]`, not the model itself
3. `.get()` returns `MyModel | None` — you must handle the `None` case
4. `.set(instance)` persists the model to disk as JSON

The `SessionSlot` intermediate is the main confusion point — `ctx.s[Model]` looks like dict access but returns a slot object, not the value.

**Resolution:** Design decision — keep the current API. The `SessionSlot` indirection is necessary because get/set are separate operations (set requires an argument, get can return None). The alternative `ctx.s.get(Model)` / `ctx.s.set(Model, instance)` would lose the generic typing advantage (IDE knows `.get()` returns `PrimitiveState | None`, not `BaseModel | None`). Document the pattern clearly with a "Working with State" section showing the common `get-or-create` pattern:
```python
state = evt.ctx.s[MyState].get() or MyState()
state.counter += 1
evt.ctx.s[MyState].set(state)
```

---

### FP-5: Condition types not discoverable from `TCondition` union

**Area:** Conditions / Discoverability

`TCondition` is a union of 12 types: `Tool | FilePath | Command | Content | Agent | UsedSkill | ReadFile | TestFile | TouchedFile | RanCommand | InPlanMode | CustomCondition`. A developer writing `only_if=[???]` has no way to discover available conditions except reading the source or the docs. IDE autocomplete on `TCondition` shows the union type but doesn't list its members.

The condition names are sometimes confusing:
- `Command` vs `RanCommand` — `Command` matches the *current* event's command; `RanCommand` matches commands in the *transcript history*. This distinction is critical but the names don't signal it.
- `FilePath` vs `TouchedFile` — same pattern: `FilePath` matches the current event's file, `TouchedFile` checks transcript history.
- `ReadFile` — reads as "read a file" but means "has this file been read in the transcript?"
- `TestFile` — reads as a type of file, but it's a condition that checks whether the current event targets a test file.

**Resolution:** The naming follows a consistent pattern once you understand it: bare nouns (`Tool`, `FilePath`, `Command`, `Content`, `TestFile`) match the *current event*; past-tense/compound names (`RanCommand`, `ReadFile`, `TouchedFile`, `UsedSkill`, `InPlanMode`) match *transcript history*. This is a reasonable convention. Renaming would break the v1-compatible API.

Fix via documentation:
- Add a "Conditions Reference" table in the docs with columns: Name, Matches, Scope (event vs transcript)
- Add inline examples in condition docstrings showing both `only_if` and `skip_if` usage

---

### FP-6: `nudge()` has too many overloaded behaviors controlled by `block=`

**Area:** Primitives / API design

`nudge(message, block=True)` becomes a gate (blocks execution). This is surprising — a "nudge" that blocks is conceptually different from a nudge that warns. The framework provides `gate()` as a convenience wrapper (`gate(message) == nudge(message, block=True)`), but:
1. The `gate()` function is just `def gate(message, **kwargs): nudge(message, block=True, **kwargs)` — a trivial wrapper
2. Both share the same handler code, signal matching, and echo suppression
3. The default `events` differ based on `block`: gates default to `Stop | SubagentStop`, nudges default to `PostToolUse` (if signals) or `PreToolUse` (if no signals)

A developer writing `nudge("stop doing that", block=True)` will be confused when it defaults to `Stop` events instead of `PreToolUse`. The implicit event selection based on `block` + `signals` presence is three-way logic that must be memorized.

**Resolution:** Design decision — keep the current API. The `gate()` convenience wrapper exists specifically so developers don't need to write `nudge(..., block=True)`. The default event selection (`Stop` for gates, `PostToolUse` for signal nudges, `PreToolUse` for non-signal nudges) matches the overwhelmingly common use case. Attempting to gate something at `PreToolUse` rarely makes sense (you'd use `block_command` instead). Document the event defaults explicitly in the `nudge()` and `gate()` docstrings, and note that `events=` overrides the default.

---

### FP-7: Inline test infrastructure (`TTest`, `Input`, `Block`, `Warn`, `Allow`) lacks typing guidance

**Area:** Testing

The inline test dict type `TTest = dict[str | Input, Block | Warn | Allow]` uses `str` keys (for v1 session-key compatibility) and `Input` keys (for v2 structured tests). In `run_inline_tests`, `str` keys are skipped with "Session keys not supported". A developer encountering `TTest` sees `dict[Any, Any]` from the `types.py` definition (it's actually `dict[Any, Any]` there!) and gets no IDE guidance on valid keys/values.

The `Input` dataclass has 8 optional fields (all `None`-defaulting), making it unclear which fields matter for which event type. Writing `Input(command="git stash")` for a `PreToolUse` hook works, but `Input(content="def foo()")` for a `PostToolUse` lint hook requires knowing that content maps to `new_string` on `Edit` events.

**Resolution:** The `TTest` type alias in `types.py` is `dict[Any, Any]` to avoid circular imports — the real type lives in `testing/types.py` as `dict[str | Input, Block | Warn | Allow]`. This should be documented.

For `Input`, add a "Testing Guide" doc section showing which fields apply to which event types:
- `command=` → Bash tool events
- `file=`, `content=`, `old=` → Edit/Write tool events
- `tool=` → overrides tool name
- `prompt=` → UserPromptSubmit events
- `transcript=` → sets transcript fixture for transcript-aware hooks

Additionally, the fix to `run_inline_tests` (FP-8) addresses the LLM hook testing gap.

---

### FP-8: LLM hook inline tests depend on real subprocess calls — fail without codex CLI

**Area:** Testing / Reliability

**This issue has been fixed as part of this audit.**

`run_inline_tests` called `execute_hook(entry, evt)` which ran the full handler including `call_llm` via `codex exec` subprocess. This meant inline tests for `llm_gate`/`llm_nudge` hooks either:
- Failed when codex CLI was unavailable (the `try/except` in `llm_evaluate` catches the error, returns `None`, causing `Warn` expectations to fail)
- Were non-deterministic when codex was available (LLM might not agree with the signal match)

**Fix applied:** `run_inline_tests` now stubs `evt.ctx.call_llm` with a deterministic mock that returns a positive verdict (e.g., `NudgeVerdict(fire=True, reasoning="inline test stub")`). This makes inline tests verify the signal/condition pipeline deterministically. The LLM verdict is assumed to agree — inline tests validate that the *right signals trigger the right hooks*, not that the LLM produces the right verdict.

---

### FP-9: `Command` name collision — condition type vs command module class

**Area:** Naming

`captain_hook.types.Command` is a condition dataclass (matches current event's bash command against a regex pattern). `captain_hook.command.Command` is the parsed command class (with `executable`, `args`, `redirects`, `program` etc.). Both are exported in `__all__`.

A developer writing `from captain_hook import Command` gets the *condition* type. To get the parsed command class, they must know it also exists under the same name — but the first import shadows it.

**Resolution:** Design decision — this is a real collision but renaming either would be a significant API change. The condition `Command` is used far more frequently (in `only_if=[Command(r"git stash")]`), so it wins the unqualified import. The parsed `Command` from `command.py` is mostly used internally (by `CommandLine.parse`, `Transcript.commands`, etc.) and rarely needed by hook authors directly. Document the collision and recommend:
- `from captain_hook import Command` — for conditions (common)
- `from captain_hook.command import Command as ParsedCommand` — for parsed commands (rare)

---

### FP-10: `InMissionMode` condition reads settings JSON from transcript path — fragile coupling

**Area:** Conditions / Architecture

`InMissionMode` (referenced in `VAL-CROSS-016`) works by deriving a `.settings.json` path from the transcript path and checking for mission-related tags. This couples the condition to Claude Code's internal file layout. If the settings file format changes or the path convention shifts, this condition silently stops working.

**Resolution:** Design decision — this coupling is intentional and necessary. `InMissionMode` exists specifically to detect Factory Droid mission context, which is only knowable via Claude Code's session settings. The condition is used in `skip_if` to suppress hooks during mission execution (where hooks would interfere with orchestrated agent workflows). The fragile coupling is acceptable because:
1. The condition fails open (no settings file → condition doesn't match → hook runs)
2. It's only used internally, not exposed as a "build your own" pattern
3. The Claude Code session layout is stable across versions

---

### FP-11: `discover_hooks` silently swallows import errors in hook modules

**Area:** Error messages / Debugging

`HookApp.discover_hooks()` uses `importlib.import_module` / `importlib.reload` to load hook modules. If a hook module has an import error (e.g., missing dependency, syntax error), the error propagates and crashes the entire hook system. There's no graceful degradation or helpful error message pointing to which hook file caused the problem.

Conversely, if a hook module loads but fails to register any hooks (e.g., `get_current_app()` raises because the module is imported outside a discovery context — see gotchas doc), the failure is silent. The hook simply doesn't exist.

**Resolution:** The crash-on-import-error behavior is correct — a hook that can't load is a bug, not a soft failure. Silent crashes are worse than loud ones. However, the error message could be improved:
- Wrap each module import in a try/except that re-raises with the module path: `ImportError: Failed to load hook module 'src.style': ModuleNotFoundError: No module named 'foo'`
- Add a `--verbose` flag to the CLI that logs each module as it's discovered

---

### FP-12: `tokens_to_regex` is a public export but has no clear entry point or documentation

**Area:** Discoverability

`tokens_to_regex` converts a list of string tokens to a regex pattern, with `*` → `\S+` and `|`-separated tokens → alternations. It's used internally by `block_command` and `warn_command` to allow pattern shorthand like `["git", "stash"]` → `r"git\s+stash"`. But it's exported in `__all__` with no indication of what it does or why a hook author would use it directly.

**Resolution:** Keep in `__all__` (it's useful for advanced users building custom command matchers) but add a docstring explaining the token language and its relationship to `block_command`/`warn_command`.

---

### FP-13: CLI `--help` lacks subcommand descriptions and usage examples

**Area:** CLI / Discoverability

Running `python -m captain_hook --help` shows:
```
usage: captain-hook [-h] [--hooks HOOKS] [--root ROOT] {run,generate-settings,test} ...
```

No description of what each subcommand does. No usage examples. A developer must read the source to understand that:
- `run EVENT` reads JSON from stdin and dispatches it through registered hooks
- `generate-settings` outputs Claude Code settings JSON for `.claude/settings.local.json`
- `test` runs inline tests from registered hooks

**Resolution:** Add `help=` text to each `add_parser()` call and `description=` to the top-level parser. This is a one-line fix per subcommand.

---

## Cross-Cutting Evaluations

### `hook()` / `@on()` split

**Verdict:** Well-designed. The split maps to the two real use cases: most hooks are declarative (message + conditions), and complex hooks need handler functions. The primitives (`nudge`, `gate`, `lint`, etc.) add a third tier for common patterns. The three-tier approach (declarative → primitive → handler) provides the right abstraction at each level.

### Condition discoverability

**Verdict:** Needs documentation help, not API changes. The 12 condition types have a consistent naming pattern (current-event nouns vs transcript-history compounds) that becomes intuitive once explained. A conditions reference table would solve 90% of discoverability issues.

### Prompt builder value

**Verdict:** Justified. `Prompt().system("...").context("tag", content).ask("...")` is cleaner than manual `textwrap.dedent` + XML tag construction. The `context(tag, None)` auto-skip for empty content prevents boilerplate conditionals. The `__str__` method produces well-formatted prompts. Worth the abstraction.

### `ctx.state[Model]` obviousness

**Verdict:** Non-obvious but correct. The generic typing makes it worth the indirection — IDE autocomplete through `ctx.s[MyModel].get()` returns `MyModel | None`, not `BaseModel | None`. Document the pattern, don't change it.

### Error messages

**Verdict:** Adequate for crashes (Python tracebacks are informative), weak for misuse. `get_current_app()` raises `RuntimeError("No HookApp in current context")` — correct but could suggest the fix (import your hook module during `discover_hooks`). Other error paths (wrong condition type, missing `message=` on `hook()`) produce generic Python `TypeError`s that don't guide the user.

### Signal system

**Verdict:** Powerful and well-integrated. `Signals(patterns=[Signal(pattern=..., weight=N), NlpSignal(...)], threshold=T, window=W)` is a clean, composable API. Echo suppression via content hashing prevents re-firing on same text. The `cite_message` auto-appends "Triggered by:" context. The main DX issue is the learning curve — signals, NLP, echo suppression, and window slicing are four concepts to learn together.

---

## Summary

| # | Friction Point | Area | Resolution |
|---|---------------|------|------------|
| 1 | `Prompt`/`PromptMessage` dual identity | Naming | Document alias; no change |
| 2 | `hook()` vs `app.hook()` vs `@app.on()` confusion | Registration | Document 3 patterns in quickstart |
| 3 | 140 flat exports overwhelm autocomplete | Imports | Document groupings; consider `prelude` module |
| 4 | `ctx.s[Model]` class-keyed state non-obvious | State | Document get-or-create pattern |
| 5 | Conditions not discoverable from `TCondition` | Conditions | Add reference table with scope labels |
| 6 | `nudge()` implicit event selection based on flags | Primitives | Document defaults in docstrings |
| 7 | `TTest`/`Input` lack typing guidance | Testing | Document field-to-event mapping |
| 8 | LLM inline tests depended on real subprocess | Testing | **Fixed**: stub `call_llm` in `run_inline_tests` |
| 9 | `Command` name collision (condition vs parsed) | Naming | Document collision; recommend qualified import |
| 10 | `InMissionMode` fragile filesystem coupling | Conditions | Design decision; fails open, acceptable |
| 11 | `discover_hooks` error messages unhelpful | Errors | Wrap imports with module-path context |
| 12 | `tokens_to_regex` undocumented export | Discoverability | Add docstring |
| 13 | CLI `--help` lacks subcommand descriptions | CLI | Add help text to argparse |
