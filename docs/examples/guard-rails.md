# Guard Rails

*Block dangerous shell commands before they execute.*

## The problem

Your team uses `jj` for version control. Claude keeps running `git stash`, `git rebase`, and `git push --force` -- commands that conflict with your workflow and can destroy history. You also want to prevent `rm -rf /` and similar destructive commands from ever reaching the shell.

## The solution

### Block a specific command

The simplest case: block `git stash` using a token list. Each token is matched with flexible whitespace between them.

```python
from captain_hook import block_command, Input, Block, Allow

block_command(
    ["git", "stash"],
    reason="Use jj shelve instead of git stash",
    hint="Run: jj shelve",
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
    },
)
```

The token list `["git", "stash"]` becomes the regex `git\s+stash`, matching any amount of whitespace between the two words. It also matches `git stash pop` because the pattern matches a prefix of the command.

### Block with a regex pattern

For more control, pass a regex string directly. This blocks force-push while allowing regular push:

```python
block_command(
    r"git\s+push\s+--force\b",
    reason="Force-push rewrites remote history",
    hint="Use --force-with-lease for a safer push",
    tests={
        Input(command="git push --force origin main"): Block(),
        Input(command="git push --force-with-lease"): Allow(),
        Input(command="git push origin main"): Allow(),
    },
)
```

!!! tip
    The `\b` word boundary prevents the pattern from matching `--force-with-lease`, which is the safer alternative you actually want.

### Block destructive commands

Add protection against recursive deletion:

```python
block_command(
    ["rm", "-rf", "*"],
    reason="Recursive force-delete is forbidden",
    hint="Delete files individually or use a safer alternative",
    tests={
        Input(command="rm -rf /"): Block(),
        Input(command="rm -rf /tmp/build"): Block(),
        Input(command="rm file.txt"): Allow(),
    },
)
```

The `"*"` token becomes `\S+`, matching any single non-whitespace argument. So `["rm", "-rf", "*"]` matches `rm -rf` followed by anything.

### Block all git mutation commands

Combine multiple rules to enforce jj across the board:

```python
block_command(["git", "stash"], reason="Use jj shelve", hint="Run: jj shelve")
block_command(["git", "rebase"], reason="Use jj rebase", hint="Run: jj rebase")
block_command(["git", "checkout"], reason="Use jj new or jj edit", hint="See: jj help")
block_command(["git", "commit"], reason="Use jj commit", hint="Run: jj commit")
block_command(
    r"git\s+push\s+--force\b",
    reason="Force-push forbidden",
    hint="Use --force-with-lease",
)
```

### What the agent sees

When a hook blocks a command, the agent receives a JSON response like this:

```json
{
  "hook_id": "block_command__use_jj_shelve_instead_of_git_stash",
  "action": "block",
  "message": "BLOCKED: Use jj shelve instead of git stash. Run: jj shelve."
}
```

The command never reaches the shell. The agent sees the block message and the hint, giving it a clear path forward.

!!! note
    `block_command` registers a `PreToolUse` hook scoped to the `Bash` tool. The command regex is tested against the full command string before execution. If you need to warn instead of block, use `warn_command` with the same API.
