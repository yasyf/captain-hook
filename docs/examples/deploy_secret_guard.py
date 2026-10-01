"""Block irreversible deploys and secret-exfiltration commands before they run."""

from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Event,
    Input,
    LambdaCondition,
    Tool,
    block_command,
    hook,
)

block_command(
    ["terraform", "destroy"],
    reason="terraform destroy tears down live infrastructure",
    hint="Run `terraform destroy -target=<resource>` against a single resource",
    tests={
        Input(command="terraform destroy"): Block(),
        Input(command="terraform destroy -target=module.cache"): Block(),
        Input(command="terraform plan"): Allow(),
    },
)

block_command(
    ["kubectl", "delete", "namespace|ns"],
    reason="Deleting a namespace deletes everything inside it",
    hint="Run `kubectl delete <kind> <name>` for the specific resource",
    tests={
        Input(command="kubectl delete namespace prod"): Block(),
        Input(command="kubectl delete pod web-123"): Allow(),
    },
)


SecretsExfil = LambdaCondition(
    lambda evt: any(s in evt.command.raw for s in ("get-secret-value", "AWS_SECRET", "PRIVATE_KEY"))
)


hook(
    Event.PreToolUse,
    message="BLOCKED: this prints a secret into the transcript. Read it from your secret store at runtime.",
    block=True,
    only_if=[Tool("Bash"), SecretsExfil],
    tests={
        Input(command="aws secretsmanager get-secret-value --secret-id db"): Block(pattern="secret"),
        Input(command="env | grep AWS_SECRET_ACCESS_KEY"): Block(pattern="secret"),
        Input(command="aws s3 ls"): Allow(),
    },
)
