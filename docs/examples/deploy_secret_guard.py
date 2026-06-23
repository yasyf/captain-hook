"""Block irreversible deploys and secret-exfiltration commands before they run."""

from __future__ import annotations

from dataclasses import dataclass

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    Input,
    Tool,
    block_command,
    hook,
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


@dataclass(frozen=True)
class SecretsExfil(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        cmd = evt.command or ""
        return "get-secret-value" in cmd or "AWS_SECRET" in cmd or "PRIVATE_KEY" in cmd


# Block commands that would copy a secret into the transcript or a log.
hook(
    Event.PreToolUse,
    message="BLOCKED: this prints a secret into the transcript. Read it from your secret store at runtime.",
    block=True,
    only_if=[Tool("Bash"), SecretsExfil()],
    tests={
        Input(command="aws secretsmanager get-secret-value --secret-id db"): Block(pattern="secret"),
        Input(command="env | grep AWS_SECRET_ACCESS_KEY"): Block(pattern="secret"),
        Input(command="aws s3 ls"): Allow(),
    },
)
