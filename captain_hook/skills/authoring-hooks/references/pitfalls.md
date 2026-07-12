# Hook Authoring Pitfalls

Each rule below is a shipped failure mode. Check the draft against all five before
running `capt-hook test`, and against #3-#4 again before calling the hook done.

## 1. `gate()` and `nudge()` are one-shot nudges, never enforcement

Both primitives throttle by design: a bare `nudge()` or `gate()` defaults to
`max_fires=1` per session (`nudge` with `signals=` gets 3), and `gate()` additionally
skips when it already fired this turn (`fired_this_turn`). After the cap, the hook goes
**silent** — by the second violation in a session, a `gate()` guard no longer guards.

That is the right shape for advice ("remember to run the tests") and exactly the wrong
shape for invariants. An always-enforcing guard — anything protecting security,
correctness, or irreversible actions — must be:

```python
hook(Event.PreToolUse, only_if=[Tool("Bash"), Command(r"git\s+push\s+--force(?!-)")],
     message="...", block=True)          # fires every time, no cap
```

or `block_command(...)`, which is the same thing with the message rendered for you.
Never use `gate()` for security or correctness enforcement.

## 2. Bare defaults: `nudge` → PreToolUse, `gate` → Stop | SubagentStop

With no `events=`:

- `nudge(...)` registers on **PreToolUse** (on **PostToolUse** when `signals=` is set).
- `gate(...)` registers on **Stop | SubagentStop** — it is a done-criterion check, not
  a tool-call interceptor.

A `gate` meant to intercept a command needs `events=Event.PreToolUse` plus
`only_if=[Tool("Bash"), Command(...)]` explicitly; left on its default it fires only
when the agent tries to stop, long after the command ran.

## 3. A broken hook module blocks the user's session

Claude Code treats a hook process exit code 2 as "block". You never touch the hook
command wiring — the captain-hook plugin owns it — so the failure mode left is a hook
*module* that raises at import: it errors on **every** event and wedges the session,
and the user cannot even ask Claude to fix it, because every turn re-fires the broken
hook.

`uvx --isolated capt-hook test` catches a broken module before it reaches a live session: an
import error fails the run and the traceback names the line.

## 4. `uvx --isolated capt-hook test` green BEFORE the hook goes live — always

There is no wiring step to hide behind: every event is already registered, so a new
file is dispatched as soon as a session loads it. The inline tests are what proves
the hook imports, its conditions match what you think they match, and its message
renders — *before* the dispatcher runs it against live sessions. An untested hook
file is #3 waiting to happen. Run:

```bash
uvx --isolated capt-hook test
```

and call the hook done only on a fully green run (exit code 0).

## 5. Over-broad conditions erode trust — condition on the narrowest pattern

A condition that matches more than the corrected behavior re-fires on unrelated and
repeated tool calls. The agent starts seeing the message on work the user never
complained about, says so ("the hook misfired — ignoring it"), and those misfire
complaints get mined and turned into fix-PRs **against the hook** — an over-broad hook
is born with its own removal ticket attached.

Condition on the narrowest pattern that still captures the correction:

- Anchor command regexes to tokens (`r"git\s+push\s+--force(?!-)"`), not substrings
  (`"force"`).
- Scope file rules with `FilePath(...)` / `SourceEdits(...)` and carve out the benign
  neighbor with `skip_if` (e.g. `TestFile()`).
- If the rule only bites in one phase (stopping, pushing, editing source), pick the
  event that phase owns instead of firing early and often.

The benign-neighbor `Allow()` test is the regression guard for this rule: make it the
*closest* non-violating input, not an obviously unrelated one.
