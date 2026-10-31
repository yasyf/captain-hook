from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook import Allow, Event, Input, LambdaCondition, Tool, Warn, hook
from captain_hook.bindings import tokens

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent

MODIFIED_PARAMETER = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+):[aAcehlPqQrstux]")


def has_modified_parameter(raw: str) -> bool:
    modes = ["plain"]
    index = 0
    while index < len(raw):
        char, mode = raw[index], modes[-1]
        if char == "\\":
            index += 2
        elif char == "'" and mode != "dq":
            end = raw.find("'", index + 1)
            index = len(raw) if end < 0 else end + 1
        elif char == '"':
            modes.pop() if mode == "dq" else modes.append("dq")
            index += 1
        elif raw.startswith("$(", index):
            modes.append("cmd")
            index += 2
        elif char == ")" and mode == "cmd":
            modes.pop()
            index += 1
        elif MODIFIED_PARAMETER.match(raw, index):
            return True
        else:
            index += 1
    return False


def writes_modified_parameter(evt: BaseHookEvent) -> bool:
    return any(not token.operator and has_modified_parameter(token.raw) for token in tokens(evt.cmd.raw))


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), LambdaCondition(writes_modified_parameter)],
    message=(
        "zsh reads `$X:r...` as a history modifier, so `$N:refs/heads/x` becomes `<value>efs/heads/x`. "
        "Brace the parameter: `${N}:refs/heads/x`."
    ),
    tests={
        Input(command="git push origin $N:refs/heads/x"): Warn(pattern="Brace the parameter"),
        Input(command='git push origin "$N:refs/heads/x"'): Warn(pattern="Brace the parameter"),
        Input(command="git cat-file -p $1:README"): Allow(),
        Input(command="git show $rev:path"): Allow(),
        Input(command="git show $rev:tools/x"): Warn(pattern="Brace the parameter"),
        Input(command="echo $(git rev-parse $sha:r)"): Warn(pattern="Brace the parameter"),
        Input(command='echo "it\'s $N:r"'): Warn(pattern="Brace the parameter"),
        Input(command='git push origin "${sha}:refs/heads/x"'): Allow(),
        Input(command="git push origin ${sha}:refs/heads/x"): Allow(),
        Input(command="echo '$N:refs'"): Allow(),
        Input(command='echo "$a: $b"'): Allow(),
        Input(command="curl host:$PORT"): Allow(),
        Input(command="echo $N:2 $N:/x"): Allow(),
        Input(command="echo \\$N:r"): Allow(),
        Input(command="echo $(echo '$N:r')"): Allow(),
        Input(command="cat <<'EOF'\n$N:r\nEOF"): Allow(),
    },
)
