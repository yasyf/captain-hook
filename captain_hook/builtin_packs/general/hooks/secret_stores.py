from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    CommandMatches,
    CommandSchema,
    Event,
    Input,
    Operand,
    Option,
    Or,
    Runs,
    Tool,
    hook,
)
from captain_hook.cmd import COMMAND_VALUE_FLAGS

if TYPE_CHECKING:
    from captain_hook import Arguments

AWS_GLOBAL_FLAGS = (
    "--debug",
    "--no-verify-ssl",
    "--no-paginate",
    "--no-sign-request",
    "--no-cli-pager",
    "--cli-auto-prompt",
    "--no-cli-auto-prompt",
)
SSM_READS = frozenset({"get-parameter", "get-parameters", "get-parameters-by-path", "get-parameter-history"})
SSM_DECRYPTS = frozenset({"--with-decryption", "--cli-input-json", "--cli-input-yaml"})
SECRETSMANAGER_READS = frozenset({"get-secret-value", "batch-get-secret-value"})
OP_ITEM_REVEALS = frozenset({"--reveal", "--otp", "--fields", "--format"})
KEYCHAIN_LOOKUPS = frozenset({"find-generic-password", "find-internet-password"})
KEYCHAIN = CommandSchema(
    "security",
    operands=(Operand("verb"), Operand("keychains", count="*")),
    options=(
        Option("attribute", tuple(f"-{letter}" for letter in "acCDGjlsrdptP")),
        Option("reveal", ("-g", "-w"), bool),
    ),
)


def aws(service: str) -> CommandSchema:
    return CommandSchema(
        "aws",
        subcommand=(service,),
        operands=(Operand("operation"),),
        options=(Option("global", COMMAND_VALUE_FLAGS["aws"]), Option("switch", AWS_GLOBAL_FLAGS, bool)),
    )


@dataclass(frozen=True, slots=True)
class OperandIs:
    name: str
    values: Collection[str]

    def __call__(self, arguments: Arguments) -> bool:
        bound = arguments.values.get(self.name, ())
        return (bound[0] if bound else "") in self.values


@dataclass(frozen=True, slots=True)
class Passes:
    flags: Collection[str]

    def __call__(self, arguments: Arguments) -> bool:
        return any(arg.partition("=")[0] in self.flags for arg in arguments.call.args)


def reveals(arguments: Arguments) -> bool:
    return bool(arguments.values.get("reveal"))


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(
            CommandMatches(aws("ssm"), only_if=(OperandIs("operation", SSM_READS), Passes(SSM_DECRYPTS))),
            CommandMatches(aws("secretsmanager"), only_if=(OperandIs("operation", SECRETSMANAGER_READS),)),
            Runs("op", "read"),
            Runs("op", "inject"),
            Runs("op", "run"),
            Runs("op", "document", "get"),
            CommandMatches(CommandSchema("op", subcommand=("item", "get")), only_if=(Passes(OP_ITEM_REVEALS),)),
            CommandMatches(KEYCHAIN, only_if=(OperandIs("verb", KEYCHAIN_LOOKUPS), reveals)),
            Runs("chamber", "read"),
            Runs("chamber", "export"),
            Runs("chamber", "exec"),
            Runs("chamber", "env"),
            Runs("gcloud", "secrets", "versions", "access"),
        ),
    ],
    message=(
        "Agents never read secret values from SSM, Secrets Manager, 1Password, Keychain, chamber, or gcloud. "
        "Ask the owner, or use a non-secret source such as `aws ssm describe-parameters` for parameter names."
    ),
    block=True,
    tests={
        Input(
            command=(
                "AWS_PROFILE=core-auto aws ssm get-parameter --with-decryption "
                "--name /ci/ai-oncall/cci-service-key | openssl pkey -pubout"
            )
        ): Block(),
        Input(command="aws --profile core-auto ssm get-parameters --names /a /b --with-decryption"): Block(),
        Input(command="aws ssm --region us-west-2 get-parameters-by-path --path /ci --with-decryption"): Block(),
        Input(command="aws ssm get-parameter --cli-input-json file://req.json"): Block(),
        Input(command="aws secretsmanager get-secret-value --secret-id prod/db"): Block(),
        Input(command="op read op://Private/github/token"): Block(),
        Input(command="op inject -i config.tpl -o config.yml"): Block(),
        Input(command="op run --env-file .env -- ./deploy.sh"): Block(),
        Input(command="op item get GitHub --fields password --reveal"): Block(),
        Input(command="op --account forge item get GitHub --format json"): Block(),
        Input(command="security find-generic-password -s cci-service -w"): Block(),
        Input(command="security find-internet-password -s github.com -g"): Block(),
        Input(command="chamber read ai-oncall service-key"): Block(),
        Input(command="chamber exec ai-oncall -- env"): Block(),
        Input(command="gcloud secrets versions access latest --secret=api-key"): Block(),
        Input(command="sh -c 'aws ssm get-parameter --name /ci/key --with-decryption'"): Block(),
        Input(command="aws ssm describe-parameters --parameter-filters Key=Path,Values=/ci"): Allow(),
        Input(command="aws ssm get-parameter --name /config/region"): Allow(),
        Input(command="aws ssm get-parameters-by-path --path /ci --no-with-decryption"): Allow(),
        Input(command="aws secretsmanager list-secrets"): Allow(),
        Input(command="op item list --vault Private"): Allow(),
        Input(command="op item get GitHub"): Allow(),
        Input(command="security find-generic-password -s cci-service"): Allow(),
        Input(command="chamber list ai-oncall"): Allow(),
        Input(command="gcloud secrets list"): Allow(),
        Input(command="echo aws ssm get-parameter --with-decryption"): Allow(),
    },
)
