from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Event,
    HookResult,
    Input,
    Tool,
    block_command,
    on,
)

# Block irreversible infrastructure commands before they run.
block_command(
    r"terraform\s+destroy",
    reason="terraform destroy tears down live infrastructure",
    hint="Target a single resource, or run it in a sandbox workspace",
    tests={
        Input(command="terraform destroy"): Block(),
        Input(command="terraform destroy -target=module.cache"): Block(),
        Input(command="terraform plan"): Allow(),
    },
)

block_command(
    r"kubectl\s+delete\s+(namespace|ns)\b",
    reason="Deleting a namespace deletes everything inside it",
    hint="Delete the specific resource instead of the whole namespace",
    tests={
        Input(command="kubectl delete namespace prod"): Block(),
        Input(command="kubectl delete pod web-123"): Allow(),
    },
)


# Block commands that would copy a secret into the transcript or a log.
@on(
    Event.PreToolUse,
    only_if=[Tool("Bash")],
    tests={
        Input(command="aws secretsmanager get-secret-value --secret-id db"): Block(pattern="secret"),
        Input(command="env | grep AWS_SECRET_ACCESS_KEY"): Block(pattern="secret"),
        Input(command="aws s3 ls"): Allow(),
    },
)
def block_secret_exfil(evt: BaseHookEvent) -> HookResult | None:
    cmd = evt.command or ""
    if "get-secret-value" in cmd or "AWS_SECRET" in cmd or "PRIVATE_KEY" in cmd:
        return evt.block(
            "BLOCKED: this prints a secret into the transcript. Read it from your secret store at runtime."
        )
    return None
