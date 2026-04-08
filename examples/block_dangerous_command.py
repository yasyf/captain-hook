# Example: Blocking a dangerous command
#
# Demonstrates how to use `block_command` to prevent the agent from
# running destructive shell commands.  The hook intercepts PreToolUse
# events for Bash and denies execution when the command matches.

from captain_hook import (
    HookApp,
    block_command,
    dispatch_test,
)
from captain_hook.app import _current_app

app = HookApp()
token = _current_app.set(app)

block_command(
    ["rm", "-rf", "*"],
    reason="Recursive force-delete is forbidden",
    hint="Delete files individually or use a safer alternative",
)

block_command(
    r"git\s+push\s+--force",
    reason="Force-push rewrites remote history",
    hint="Use --force-with-lease for a safer push",
)

_current_app.reset(token)

result = dispatch_test(app, "PreToolUse", tool="Bash", command="rm -rf /tmp")
assert result is not None
output = result["hookSpecificOutput"]
assert output["permissionDecision"] == "deny"
assert "forbidden" in output["permissionDecisionReason"].lower()

safe = dispatch_test(app, "PreToolUse", tool="Bash", command="ls -la")
assert safe is None

force_push = dispatch_test(app, "PreToolUse", tool="Bash", command="git push --force origin main")
assert force_push is not None
assert force_push["hookSpecificOutput"]["permissionDecision"] == "deny"

safe_push = dispatch_test(app, "PreToolUse", tool="Bash", command="git push origin main")
assert safe_push is None

print("✓ Dangerous commands blocked, safe commands allowed")
