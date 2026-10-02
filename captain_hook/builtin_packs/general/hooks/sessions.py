from __future__ import annotations

import re
from functools import partial
from pathlib import PurePath
from typing import TYPE_CHECKING

from cc_transcript.command import PAYLOAD_DEPTH_LIMIT

from captain_hook import Allow, Block
from captain_hook.bindings import Ref, Resolved, segments
from captain_hook.builtin_packs.general.hooks._sessions import (
    ARG_TOKEN_BREAK,
    GUARDED_PROGRAMS,
    INLINE_OWNER_ANSWER,
    INLINE_OWNER_TERMINAL,
    KILL_FIX,
    LAUNCHERS,
    NEGATIVE_TARGET,
    RENICE_FIX,
    Scan,
    Unreadable,
    answer_names,
    applescripts,
    block_first,
    clip,
    describe,
    describe_target,
    double_quoted,
    first_operand,
    guard,
    guarded,
    head_reason,
    hidden_behind,
    hosts_agent,
    is_agent,
    literal_pid,
    nested,
    pid_verdict,
    shell_scripts,
    spell,
    unresolvable,
)
from captain_hook.command_schemas import KILL, LAUNCHCTL, ORCA, PMSET, RENICE, SOFTWAREUPDATE, TMUX
from captain_hook.guard_literal import QUOTING_CHARS
from captain_hook.util.shell import SHELLS

if TYPE_CHECKING:
    from cc_transcript.command import Word

    from captain_hook import HookResult, ToolRewriteEvent
    from captain_hook.builtin_packs.general.hooks._sessions import Facts, Unparsed
    from captain_hook.cmd import Call
    from captain_hook.command_schema import Arguments, Scalar

CRITERIA_PROGRAMS = frozenset({"pkill", "killall", "killall5", "skill", "snice", "kill-port"})
PKILL_CRITERIA_SHORT = frozenset("fxnoiluUgGtPacvF")
PKILL_CRITERIA_LONG = frozenset(
    {
        "--full",
        "--exact",
        "--newest",
        "--oldest",
        "--ignore-case",
        "--inverse",
        "--count",
        "--list-name",
        "--list-full",
        "--euid",
        "--uid",
        "--group",
        "--pgroup",
        "--session",
        "--terminal",
        "--parent",
        "--ns",
        "--nslist",
        "--pidfile",
        "--logpidfile",
        "--cgroup",
        "--runstates",
        "--ignore-ancestors",
        "--require-handler",
    }
)
ORCA_VM_RUN_FLAGS = frozenset({"--provision", "--connect"})
PACKAGE_RUNNERS = frozenset({"npx", "bunx", "pnpx", "pnpm", "yarn"})
CRITERIA_FIX = (
    "Find the pid with `pgrep -fl <pattern>`, verify it with `ps -o pid,ppid,pgid,lstart,command -p <pid>`, and "
    "run `kill <pid>` alone."
)
SHUTDOWNS = frozenset({"shutdown", "reboot", "halt", "poweroff"})
FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
ORCA_GROUPS = frozenset(
    {
        "account",
        "agent",
        "agent-context",
        "artifacts",
        "automations",
        "capture",
        "claude-teams",
        "computer",
        "cookie",
        "diagnostics",
        "emulator",
        "environment",
        "file",
        "help",
        "host",
        "linear",
        "open",
        "orchestration",
        "project",
        "repo",
        "search",
        "serve",
        "skills",
        "status",
        "storage",
        "tab",
        "terminal",
        "vm",
        "worktree",
    }
)
ORCA_ENDINGS = {
    "terminal": frozenset({"close", "stop"}),
    "worktree": frozenset({"rm", "remove", "delete"}),
    "orchestration": frozenset({"worker-stop", "worker-release", "coordinator-stop", "run-stop"}),
}
ORCA_PAYLOADS = {
    "hotkey": "key",
    "press-key": "key",
    "type-text": "text",
    "paste-text": "text",
    "set-value": "value",
    "perform-secondary-action": "action",
}
HELP_FLAGS = frozenset({"--help", "-h"})
ORCA_READ = "Read sessions with `orca terminal list|show|read|wait` and leave ending them to the owner."
ORCA_CLOSE_SCOPES = frozenset({"all", "worktree", "tab"})
OWNER_FIX = "Add `# ccx:owner-authorized=<cc-notes answer id>`; its body must name this terminal id."
KEY_ALIASES = {
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    "escape": "esc",
}
END_OF_SESSION_CHORDS = frozenset(
    frozenset(chord)
    for chord in (
        ("cmd", "q"),
        ("ctrl", "q"),
        ("cmd", "w"),
        ("cmd", "shift", "w"),
        ("cmd", "alt", "esc"),
        ("ctrl", "c"),
        ("ctrl", "d"),
    )
)
END_OF_SESSION_ACTION = re.compile(r"(?i)\b(quit|close|kill|stop|terminate|interrupt|remove|delete)\b")
END_OF_SESSION = frozenset(
    {"exit", "/exit", "/quit", "logout", "\x03", "\x04", "^c", "^d", "c-c", "c-d", "\\x03", "\\x04"}
)
LAUNCHCTL_ENDINGS = frozenset(
    {"bootout", "kill", "kickstart", "stop", "remove", "unload", "disable", "reboot", "asuser", "bsexec"}
)
APPLESCRIPT_ENDING = re.compile(r'(?i)\b(quit|log ?out|restart|shut ?down|sleep)\b|keystroke\s+"q"')
PMSET_ENDINGS = frozenset({"sleepnow", "restart", "halt", "sleep"})
PMSET_SCHEDULES = frozenset({"shutdown", "restart", "sleep", "poweroff"})
TMUX_ENDINGS = frozenset({"kill-server", "kill-session", "kill-pane", "kill-window"})
STDIN_SCRIPTS = frozenset({"-", "/dev/stdin", "/dev/fd/0"})


@guard(
    tests={
        guarded(command="read p; $p sleep; ls ~/.orca"): Block(pattern="named at run time"),
        guarded(command="p=$(echo pkill); $p sleep"): Block(pattern="built from text the guard cannot read"),
        guarded(command='P=$(ls | head -1); [ -z "$P" ] && P=$(find ~ -name python); "$P" -c x'): Block(
            pattern="text the guard cannot read"
        ),
        guarded(command="C=$(printf '%s%s' pk ill); \"$C\" claude; orca status"): Block(
            pattern="text the guard cannot read"
        ),
        guarded(command='X=echo; V=X; read "$V" <<<pkill; "$X" claude; ls ~/.orca'): Block(pattern="named at run time"),
        guarded(command='X=echo; V=X; printf -v "$V" pkill; "$X" claude; ls ~/.orca'): Block(
            pattern="named at run time"
        ),
        guarded(command='X=echo; R=read; $R X <<<pkill; "$X" claude; ls ~/.orca'): Block(pattern="named at run time"),
        guarded(command='X=echo; R=$(echo read); $R X <<<pkill; "$X" claude; ls ~/.orca'): Block(
            pattern="named at run time"
        ),
        guarded(command='for P in /usr/bin/[p]kill; do "$P" -f claude; done'): Block(
            pattern="text the guard cannot read"
        ),
        guarded(command='P=echo; sh -c "P=pkill; \\$P claude"'): Block(pattern="named at run time"),
        guarded(command="$W kill 14575"): Block(pattern="`kill` among its arguments"),
        guarded(command="W=timeout; $W 5 kill 14575"): Block(pattern="among its arguments"),
        guarded(command="$SHELL -c 'kill 14575'"): Block(pattern="among its arguments"),
        guarded(command="$W $(echo pkill) sleep"): Block(pattern="a command substitution among its arguments"),
        guarded(command='$W "$1"; ls ~/.orca'): Block(pattern='`"\\$1"` among its arguments'),
        guarded(command="W='timeout 5'; $W pkill sleep"): Block(pattern="`pkill` among its arguments"),
        guarded(command="f() { $CMD 14575; }; CMD=kill; f"): Block(pattern="named at run time"),
        guarded(command="p=ls; (p=kill); $p 14575"): Block(pattern="text the guard cannot read"),
        guarded(command="false && p=kill; $p 14575"): Block(pattern="text the guard cannot read"),
        guarded(command="eval 'p=ls'; $p 14575; ls ~/.orca"): Block(pattern="named at run time"),
        guarded(command='[ -z "$P" ] && kill -l'): Allow(),
        guarded(command="false && p=ls; $p 14575; ls ~/.orca"): Block(pattern="text the guard cannot read"),
        guarded(command="cat <<'EOF'\np=ls\nEOF\n$p kill 14575"): Block(),
        guarded(command="{kill,14575}"): Block(pattern="expands at run time"),
        guarded(command="{pkill,-x,sleep}"): Block(),
        guarded(command="kill{,} 14575"): Block(),
        guarded(command="/bin/{kill,} 14575"): Block(),
        guarded(command="[k]ill 14575"): Block(),
        guarded(
            command=(
                "S=~/.claude/scratch/release-v3; sed -i '' '/R400 proof: plat/d' $S/desk-deadlines.txt; cd "
                "/Users/yasyf/.orca/workspaces/monorepo/monorepo/sole && $S/desk-loop.sh 2>&1 | cut -c1-900"
            )
        ): Allow(),
        guarded(
            command=(
                'AB="agent-browser --session prpack-check"; $AB open "http://127.0.0.1:61118/p/x" >/dev/null 2>&1; '
                "$AB eval \"(() => { const el = [...document.querySelectorAll('*')].find(e => e.textContent); "
                'return !!el; })()" 2>&1 | tail -1'
            )
        ): Allow(),
        guarded(
            command=(
                "F=/Users/yasyf/.claude/scratch/release-v3/watch-28635-28637.sh; "
                "sed -i '' 's/--no-watch/--watch/' $F; $F"
            )
        ): Allow(),
        guarded(
            command=(
                'cd ~/.claude/worktrees/captain-hook/hook-lint-builtins && CA="/Users/yasyf/.claude/plugins/cache/'
                'skills/codex/1.14.0/skills/codex/../../bin/codex-ask"; "$CA" --lane review --schema findings - '
                "<<'Q' 2>&1 | tail -40\nReview the builtin packs.\nQ"
            )
        ): Block(pattern="text the guard cannot read"),
        guarded(
            command=(
                'CA="/Users/yasyf/.claude/plugins/cache/skills/codex/1.14.0/skills/codex/../../bin/codex-ask"; '
                'cd ~/.claude/worktrees/captain-hook/hook-lint-builtins && "$CA" --lane review --schema findings - '
                "<<'Q' 2>&1 | tail -40\nReview the builtin packs.\nQ"
            )
        ): Allow(),
        guarded(
            command=(
                "CS=/Users/yasyf/.claude/plugins/data/cc-slack-forge/bin/cc-slack\n$CS reply --channel C0BQ --thread "
                "1790878010.813989 --no-watch --text 'Thanks for accepting the agreement.' 2>&1"
            )
        ): Allow(),
        guarded(
            command=(
                "cd /Users/yasyf/.orca/workspaces/monorepo/monorepo/sole; $HOME/.claude/plugins/cache/skills/"
                "long-running/0.6.49/bin/ledger.py inbox --ledger 829f --take 2>&1 | cut -c1-600"
            )
        ): Allow(),
        guarded(
            command=(
                "cd /Users/yasyf/.orca/workspaces/monorepo/monorepo/v3-incident-api-1n80-fix-base && "
                "B=$(realpath ../v3-incident-api-1n7z-fix-base/node_modules/.bin/biome); $B --version"
            )
        ): Block(pattern="text the guard cannot read"),
        guarded(command='timeout 20 "$CP" outcomes --no-doc --session d0bf 2>&1; ls ~/.orca'): Allow(),
        guarded(
            command=(
                "cd /Users/yasyf/.orca/workspaces/monorepo/monorepo/sole; L=/Users/yasyf/.claude/plugins/cache/skills/"
                "long-running/0.6.49/bin/ledger.py; S=$(date +%s); $L refresh --ledger 829f --repo Forge-AI/monorepo "
                "</dev/null 2>&1 | tail -5; $L reconcile --ledger 829f --checkout $PWD </dev/null 2>&1 | tail -25; "
                'echo "dur=$(( $(date +%s)-S ))s"'
            )
        ): Allow(),
        guarded(command="cd /Users/yasyf/.orca/workspaces/monorepo/monorepo/sole; $L reconcile --checkout $PWD"): Block(
            pattern="`\\$PWD` among its arguments"
        ),
        guarded(command="L=; $L ./run.sh $PWD; ls ~/.orca"): Block(pattern="`\\$PWD` among its arguments"),
        guarded(command='W=timeout; $W "$1"; ls ~/.orca'): Block(pattern="named at run time"),
        guarded(command='read -r K <<< kill; W=env; $W "$K" 14575'): Block(pattern="named at run time"),
        guarded(command="read <<<kill; W=env; $W $REPLY 14575"): Block(pattern="named at run time"),
        guarded(command='read -r K <<< kill; W=caffeinate; $W "$K" 14575'): Block(pattern="among its arguments"),
        guarded(command='PWD="i /bin/kill 14575"; W=caffeinate; $W -$PWD'): Block(pattern="among its arguments"),
        guarded(command="read -r K <<< kill; W=find; $W /tmp -exec \"$K\" 14575 ';'"): Block(
            pattern="among its arguments"
        ),
        guarded(
            command=(
                'read N <<<"; -exec /bin/kill 14575 ; -exec echo"; W=find; $W /tmp -maxdepth 1 -exec echo $N {} ";"'
            )
        ): Block(pattern="among its arguments"),
        guarded(command='PWD="exec /bin/kill 14575 ;"; W=find; $W /tmp -$PWD'): Block(pattern="among its arguments"),
        guarded(command='cd /; W=find; $W "$PWD" -name x'): Block(pattern="among its arguments"),
        guarded(command='read -r K <<< kill; W=bash; $W -c "$K 14575"'): Block(),
        guarded(
            command=("for A in a b c d e f g h kill; do for B in 1 2 3 4 5 6 7 8 14575; do $A $B; done; done")
        ): Block(pattern="more than 64 command lines"),
        guarded(
            command=(
                "for d in selection dag judge trailers targets stacks images pipeline prcheck reach reads record "
                "platy render slack hold bake ledger canvas finish; do $d/run.sh; done; ls ~/.orca"
            )
        ): Allow(),
        guarded(
            command=(
                "B=$(ls /Users/yasyf/.orca/workspaces/monorepo/monorepo/sole/node_modules/.bin/biome 2>/dev/null || "
                "command -v biome) && $B check infra/ci/src/verbs/release-stacks.ts 2>&1 | tail -25"
            )
        ): Block(pattern="text the guard cannot read"),
        guarded(
            command=(
                'for c in "bun infra/graph.ts" "bun infra/k8s.ts delivery"; do eval $c || echo "FAILED $c"; done; '
                "ls ~/.orca"
            )
        ): Allow(),
        guarded(command="X=echo; X[0]=pkill; $X claude"): Block(pattern="text the guard cannot read"),
        guarded(command="X=echo; printf -v X pkill; $X claude"): Block(pattern="named at run time"),
        guarded(command="X=echo; read 'X' <<<pkill; $X claude"): Block(pattern="named at run time"),
        guarded(command="X=echo; declare -n Y=X; Y=pkill; $X claude"): Block(pattern="named at run time"),
        guarded(command="X=pkill; X=echo | cat; $X claude"): Block(),
        guarded(command="X=pkill; X=echo & $X claude"): Block(),
        guarded(command="X=pk; X+=ill; $X claude; ls ~/.orca"): Block(pattern="named at run time"),
        guarded(command="X='[p]kill'; $X -x claude"): Block(pattern="expands at run time"),
        guarded(command="X='{p,q}kill'; $X -x claude"): Block(pattern="expands at run time"),
        guarded(command='X=echo; function f() { "$X" claude; }; X=pkill; f'): Block(pattern="named at run time"),
        guarded(command='X=echo; f() { echo }; "$X" claude; }; X=pkill; f'): Block(pattern="named at run time"),
        guarded(command='for X in {echo,pkill}; do "$X" claude; done'): Block(pattern="text the guard cannot read"),
        guarded(command="PWD=/tmp; cd /; $PWD/kill 14575"): Block(pattern="named at run time"),
        guarded(
            command=(
                "S=/Users/yasyf/.claude/scratch/release-v3; cat > $S/briefs/s3-2.md <<'EOF'\n# s3-2 brief\n"
                "Report lines to orca-desk-from-workers.md, then worker_done.\nEOF\n"
                "cat $S/briefs/common-rules.md >> $S/briefs/s3-2.md\n"
                "ORCA_LAUNCH_BOOT_SECONDS=180 $S/launch-explicit.sh s3-2 sonnet high $S/briefs/s3-2.md 2>&1 | tail -3"
            )
        ): Allow(),
        guarded(
            command=(
                "false && S=/tmp; cat > $S/briefs/s3-2.md <<'EOF'\nReport to orca-desk-from-workers.md.\nEOF\n"
                "$S/launch-explicit.sh s3-2"
            )
        ): Block(pattern="text the guard cannot read"),
        guarded(command='cd ~/.orca/workspaces/x && for g in a.ts b.ts; do eval "timeout 120 bun $g"; done'): Allow(),
        guarded(command="~/bin/kill 14575"): Allow(),
        guarded(command="kill 14575"): Allow(),
    }
)
def opaque_command_name(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(head_reason, Scan.of(evt).calls))


def queries_path(call: Call) -> bool:
    return "command" in call.wrappers and not {"-v", "-V"}.isdisjoint(call.source.args)


def unread_reason(call: Call, arguments: Arguments) -> str:
    negative = next(
        (
            word
            for index, word in enumerate(call.command.words[1:])
            if word.value is not None
            and NEGATIVE_TARGET.fullmatch(word.value)
            and (index or not 1 <= int(word.value[1:]) <= 64)
        ),
        None,
    )
    if negative is not None:
        return describe_target(negative, negative.value)
    if arguments.unread:
        return f"it passes `{clip(arguments.unread[0].raw, 40)}`, an option the guard cannot read"
    return "it names an option or signal at run time"


def probe_pid(call: Call, word: Word, value: Scalar | None) -> bool:
    if literal_pid(word, value) is not None:
        return True
    match call.resolve(word):
        case Resolved(candidates, _):
            return all(candidate.isdecimal() for candidate in candidates)
        case _:
            return False


def kill_verdict(call: Call, facts: Facts) -> str | None:
    if call.name != "kill" or queries_path(call):
        return None
    spelling = spell(call)
    deny = partial(unresolvable, spelling, fix=KILL_FIX)
    arguments = KILL.bind(call)
    if call.substituted:
        return deny("a command substitution supplies its targets at run time")
    if "xargs" in call.wrappers:
        return deny("xargs supplies its targets from stdin")
    if not arguments.complete:
        return deny(unread_reason(call, arguments))
    values = arguments.values
    if "list" in values:
        return None
    targets = tuple(zip(arguments.words.get("targets", ()), values.get("targets", ()), strict=True))
    probing = ("probe" in values and "signal" not in values) or values.get("signal") == ("0",)
    if probing and all(probe_pid(call, word, value) for word, value in targets):
        return None
    if not targets:
        return deny("a wrapper supplies its targets") if call.wrappers else None
    word, value = targets[0]
    if (pid := literal_pid(word, value)) is not None:
        return pid_verdict(pid, spelling, facts, KILL_FIX)
    if probing:
        return (
            f"BLOCKED: `{spelling}` cannot be verified: {describe_target(word, value)}, so the probe could expand "
            "into a real signal. Pass the literal pid, or a variable the same line sets to one."
        )
    return deny(describe_target(word, value))


@guard(
    tests={
        guarded(command="Kill -9 -1"): Block(pattern="broadcasts to every process you own"),
        guarded(command="pgrep -f pr-poll | xargs kill"): Block(pattern="xargs supplies"),
        guarded(command="pgrep x | xargs kill -0"): Block(pattern="`xargs kill -0`"),
        guarded(command="kill -9 -123"): Block(pattern="negative process group 123"),
        guarded(command="kill -- -123"): Block(pattern="negative process group 123"),
        guarded(command="kill -TERM -14575"): Block(),
        guarded(command="kill -123"): Block(pattern="negative process group 123"),
        guarded(command="kill 0"): Block(pattern="whole process group"),
        guarded(command="kill -9 -1"): Block(pattern="broadcasts to every process you own"),
        guarded(command="kill $pid"): Block(pattern=r"`\$pid` is not a literal"),
        guarded(command="kill $!"): Block(),
        guarded(command="kill $$"): Block(),
        guarded(command="kill %1"): Block(pattern="job spec"),
        guarded(command="kill $(pgrep x)"): Block(pattern=r"`kill \$\(pgrep x\)` cannot be verified"),
        guarded(command="kill -9 $(pgrep -f server) 2>/dev/null"): Block(pattern="command substitution"),
        guarded(command="kill 12*"): Block(),
        guarded(command="kill 0123"): Block(),
        guarded(command="kill +5"): Block(),
        guarded(command="kill --signal=TERM 123"): Block(pattern="passes `--signal=TERM`"),
        guarded(command="kill -sTERM 123"): Block(pattern="passes `-sTERM`"),
        guarded(command="kill 14575 -l"): Block(pattern="passes `-l`"),
        guarded(command="kill 14575 -s 0"): Block(pattern="passes `-s`"),
        guarded(command="kill 14575 -n 0"): Block(),
        guarded(command="kill -9 14575 -L"): Block(),
        guarded(command="sudo kill 1743 -l"): Block(),
        guarded(command="kill 1743"): Block(pattern="the Orca PTY daemon"),
        guarded(command="for A in a b c kill; do for B in 1 2 3 4 14575; do $A $B; done; done"): Block(
            pattern="pid 1 "
        ),
        guarded(command="kill 1445"): Block(pattern="the Orca app"),
        guarded(command="kill -9 14575"): Block(pattern="an agent session"),
        guarded(command=f"kill 16002 # ccx:owner-authorized={INLINE_OWNER_ANSWER}"): Block(pattern="an agent session"),
        guarded(command="kill 15001"): Block(pattern="pid 15001"),
        guarded(command="kill 14545"): Block(pattern="a terminal host"),
        guarded(command="kill 14550"): Block(pattern="an ancestor of a protected process"),
        guarded(command="kill 900"): Block(pattern="the Captain Hook host"),
        guarded(command="kill 99999"): Block(pattern="not in the current process table"),
        guarded(command="sudo kill -9 14575"): Block(pattern="an agent session"),
        guarded(command="env kill 14575"): Block(pattern="an agent session"),
        guarded(command="nohup kill 14575"): Block(pattern="an agent session"),
        guarded(command="timeout 5 kill 14575"): Block(pattern="an agent session"),
        guarded(command="sh -c 'kill 14575'"): Block(pattern="an agent session"),
        guarded(command="~/bin/kill 14575"): Block(pattern="an agent session"),
        guarded(command="/bin/kill 14575"): Block(pattern="an agent session"),
        guarded(command="command kill 14575"): Block(pattern="an agent session"),
        guarded(command="KILL 14575"): Block(pattern="an agent session"),
        guarded(command="kill -STOP 14575"): Block(),
        guarded(command="kill -s KILL 14575"): Block(),
        guarded(command="kill -0 -9 14575"): Block(),
        guarded(command="kill -0 14575 -9"): Block(),
        guarded(command="PID='-s 9 14575'; kill -0 $PID"): Block(pattern="an agent session"),
        guarded(command='bash -c \'PID=$(printf "%s" "-s KILL 14575"); kill -0 $PID\''): Block(
            pattern="probe could expand into a real signal"
        ),
        guarded(command='kill -0 "$worker" 2>/dev/null || break'): Block(pattern="probe could expand"),
        guarded(command="kill -0 ${PID}"): Block(pattern="probe could expand"),
        guarded(command="PID=$(awk '{print $2}' /tmp/apply.pid); kill -0 $PID 2>/dev/null && echo alive"): Block(
            pattern="probe could expand"
        ),
        guarded(command="PID=14575x; kill -0 $PID"): Block(pattern="probe could expand"),
        guarded(command="X=-9; kill $X $(printf 14575)"): Block(pattern="command substitution"),
        guarded(command="kill -0 $pid 14575"): Block(pattern="probe could expand into a real signal"),
        guarded(command="kill -0 $pid$x"): Block(pattern="probe could expand into a real signal"),
        guarded(command='kill -0 "$a" 14575'): Block(pattern="probe could expand"),
        guarded(command='kill -0 "$a" "$b" 14575'): Block(),
        guarded(command='kill -0 "$(cat /tmp/server.pid)"'): Block(pattern="command substitution"),
        guarded(command='trap "kill -9 14575" EXIT'): Block(pattern="an agent session"),
        guarded(command="osascript <<'EOF'\ndo shell script \"kill -9 14575\"\nEOF"): Block(pattern="an agent session"),
        guarded(tool="mcp__runner__exec", tool_input={"command": ["kill", "-9", "14575"]}): Block(
            pattern="an agent session"
        ),
        guarded(tool="mcp__runner__exec", tool_input={"command": ["bash", "-lc", "kill -9 14575"]}): Block(),
        guarded(tool="mcp__runner__exec", tool_input={"command": "kill", "args": ["-9", "14575"]}): Block(
            pattern="an agent session"
        ),
        guarded(tool="mcp__runner__exec", tool_input={"argv": ["kill", 14575]}): Block(pattern="an agent session"),
        guarded(tool="mcp__x__exec", tool_input={"command": "kill 14575 \udc80"}): Block(),
        guarded(command="kill -0 14575"): Allow(),
        guarded(command="PID=31337; kill -0 $PID"): Allow(),
        guarded(command='PID=31337; kill -0 "$PID" 2>/dev/null || break'): Allow(),
        guarded(command="for p in 31337 31338; do kill -0 $p; done"): Allow(),
        guarded(command="kill -s 0 14575"): Allow(),
        guarded(command="kill -l"): Allow(),
        guarded(command="kill -l 9"): Allow(),
        guarded(command="kill"): Allow(),
        guarded(command="command -v kill"): Allow(),
        guarded(command="ps -p 1743"): Allow(),
    }
)
def kill_unverified_pid(evt: ToolRewriteEvent) -> HookResult | None:
    scan = Scan.of(evt)
    return block_first(evt, (kill_verdict(call, scan.facts) for call in scan.literal_calls))


def renice_verdict(call: Call, facts: Facts) -> str | None:
    if call.name != "renice" or queries_path(call):
        return None
    spelling = spell(call)
    deny = partial(unresolvable, spelling, fix=RENICE_FIX)
    arguments = RENICE.bind(call)
    if call.substituted or "xargs" in call.wrappers:
        return deny("its targets are supplied at run time")
    if not arguments.complete:
        return deny(
            f"it passes `{clip(arguments.unread[0].raw, 40)}`, an option the guard cannot read"
            if arguments.unread
            else "it names an option at run time"
        )
    if "scope" in arguments.values:
        return f"BLOCKED: `{spelling}` reprioritizes a whole process group or every process of a user. {RENICE_FIX}"
    pairs = list(zip(arguments.words.get("args", ()), arguments.values.get("args", ()), strict=True))
    if "adjust" not in arguments.values:
        if not pairs or pairs[0][1] is None or not str(pairs[0][1]).lstrip("+-").isdecimal():
            return deny("its priority operand is not a literal number")
        pairs = pairs[1:]
    if not pairs:
        return None
    word, value = pairs[0]
    if (pid := literal_pid(word, value)) is None:
        return deny(describe_target(word, value))
    return pid_verdict(pid, spelling, facts, RENICE_FIX)


@guard(
    tests={
        guarded(command="renice -n 5 -p 14575"): Block(pattern="an agent session"),
        guarded(command="renice 5 -u dev"): Block(pattern="every process of a user"),
        guarded(command="renice -n 5 -g 14575"): Block(),
        guarded(command="renice 5 $pid"): Block(pattern="`renice -n <priority> -p <pid>`"),
        guarded(command="renice -n 5 -p 99999"): Block(pattern="would reach an unrelated process"),
        guarded(command="command -v renice"): Allow(),
        guarded(command="renice 5"): Allow(),
    }
)
def renice_unverified_pid(evt: ToolRewriteEvent) -> HookResult | None:
    scan = Scan.of(evt)
    return block_first(evt, (renice_verdict(call, scan.facts) for call in scan.literal_calls))


def probes_only(call: Call) -> bool:
    if call.args[:1] != ("-0",):
        return False
    for arg in call.args[1:]:
        if not arg.startswith("-"):
            continue
        flag = arg.partition("=")[0]
        if flag in PKILL_CRITERIA_LONG:
            continue
        if flag.startswith("--") or len(flag) < 2 or not set(flag[1:]) <= PKILL_CRITERIA_SHORT:
            return False
    return True


def criteria_program(call: Call) -> str | None:
    match call.name:
        case "pkill" if probes_only(call):
            return None
        case name if name in CRITERIA_PROGRAMS:
            return name
        case "fuser" if any(
            arg == "--kill" or (arg.startswith("-") and not arg.startswith("--") and "k" in arg[1:])
            for arg in call.args
        ):
            return "fuser -k"
        case name if name in PACKAGE_RUNNERS and "kill-port" in call.args:
            return "kill-port"
        case _:
            return None


def criteria_verdict(call: Call) -> str | None:
    if (program := criteria_program(call)) is None:
        return None
    return (
        f"BLOCKED: `{program}` signals every process matching a name, pattern, port, or open file, other sessions' "
        f"agents and terminals included. {CRITERIA_FIX}"
    )


@guard(
    tests={
        guarded(
            command=(
                "pkill -x sleep 2>/dev/null; sleep 0; orca orchestration check --peek --run run-1 --json 2>&1 | "
                "head -c 600; echo; echo rc=$?"
            )
        ): Block(pattern="`pkill` signals every process matching a name"),
        guarded(command='pkill -f "never" 2>/dev/null; codex-ask --help'): Block(pattern="`pgrep -fl <pattern>`"),
        guarded(command="p=pkill; $p sleep"): Block(pattern="signals every process matching"),
        guarded(command="for p in pgrep pkill; do $p -x sleep; done"): Block(pattern="signals every process matching"),
        guarded(command="ITEMS='echo pkill'; for X in $ITEMS; do \"$X\" claude; done"): Block(),
        guarded(command="X=pkill; cat <<EOF\n\tEOF\nX=echo\nEOF\n$X claude"): Block(pattern="signals every process"),
        guarded(command="X=pkill; cat <<-EOF\n\tEOF\nX=echo\nEOF\n$X claude"): Allow(),
        guarded(command="pkill node"): Block(),
        guarded(command="pkill -0 --signal KILL -f claude"): Block(pattern="signals every process matching"),
        guarded(command="pkill -0 --signal=KILL -f claude"): Block(),
        guarded(command="pkill -0 -s KILL -f claude"): Block(),
        guarded(command="pkill -0 -SIGKILL -f claude"): Block(),
        guarded(command="pkill -0 -fs KILL claude"): Block(),
        guarded(command="pkill -0 -e claude"): Block(),
        guarded(command="pkill -0 -9 x"): Block(),
        guarded(command="pkill -f -0 x"): Block(),
        guarded(command="pkill -TERM -0 x"): Block(),
        guarded(command="sudo pkill node"): Block(),
        guarded(command="pkill -9 -f 'vite dev'"): Block(),
        guarded(command="sudo pkill -f server"): Block(),
        guarded(command='pkill -f "ledger.py watch --ledger main"; sleep 1'): Block(),
        guarded(command='kill %1 2>/dev/null; pkill -f "pr-poll.sh 42" 2>/dev/null'): Block(),
        guarded(command="/usr/bin/pkill x"): Block(),
        guarded(command="xargs pkill"): Block(),
        guarded(command="p\\kill node"): Block(),
        guarded(command="killall claude"): Block(pattern="`killall`"),
        guarded(command="sudo killall Orca"): Block(),
        guarded(command="killall5 -15"): Block(),
        guarded(command="Killall claude"): Block(),
        guarded(command="fuſer -k 3000/tcp"): Block(),
        guarded(command="fuser -k 3000/tcp"): Block(pattern="`fuser -k`"),
        guarded(command="fuser -ki -TERM /tmp/sock"): Block(),
        guarded(command="npx kill-port 3000"): Block(pattern="`kill-port`"),
        guarded(command="bunx kill-port 3000"): Block(),
        guarded(command=nested(3, "pkill -x sleep")): Block(pattern="signals every process matching"),
        guarded(command="trap 'pkill -f claude' EXIT; true"): Block(pattern="signals every process matching"),
        guarded(command="osascript -e 'do shell script \"pkill -f claude\"'"): Block(
            pattern="signals every process matching"
        ),
        guarded(tool="mcp__runner__exec", tool_input={"cmd": "pkill -x sleep"}): Block(),
        guarded(tool="mcp__runner__exec", tool_input={"command": ["echo", "hi", ";", "pkill", "-x", "sleep"]}): Block(
            pattern="signals every process matching"
        ),
        guarded(
            tool="Monitor", tool_input={"command": "pkill -f poll", "description": "x", "timeout_ms": 1000}
        ): Block(),
        guarded(command="fuser -v 3000/tcp"): Allow(),
        guarded(command="gh api rate_limit -q '.resources.core' ; date +%s; pkill -0 x 2>/dev/null; echo"): Allow(),
        guarded(command="pkill -0 -f 'ledger.py watch'"): Allow(),
        guarded(command="pkill -0 -fx --newest claude"): Allow(),
        guarded(command="pgrep -f claude"): Allow(),
        guarded(command="echo pkill -f never"): Allow(),
        guarded(tool="mcp__runner__exec", tool_input={"cmd": "pgrep -f claude"}): Allow(),
        guarded(tool="mcp__x__call", tool_input={"subject": "pkill", "mode": "x"}): Allow(),
        guarded(tool="SendMessage", tool_input={"to": "a", "message": "never pkill by name"}): Allow(),
    }
)
def signal_by_criteria(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(criteria_verdict, Scan.of(evt).literal_calls))


def shutdown_verdict(call: Call) -> str | None:
    if call.name not in SHUTDOWNS:
        return None
    return (
        f"BLOCKED: `{call.name}` ends every session on the Mac, Orca and every agent in it included. Report the need "
        "to the owner instead."
    )


@guard(
    tests={
        guarded(command="reboot"): Block(pattern="ends every session on the Mac"),
        guarded(command="sudo reboot"): Block(),
        guarded(command="sudo shutdown -r now"): Block(),
        guarded(command="ſhutdown -h now"): Block(pattern="`shutdown`"),
        guarded(command="halt"): Block(),
        guarded(command="poweroff"): Block(),
        guarded(tool="mcp__srv__Bash", tool_input={"command": "reboot"}): Block(),
        guarded(command="echo reboot later"): Allow(),
    }
)
def shutdown_ends_sessions(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(shutdown_verdict, Scan.of(evt).literal_calls))


def orca_bound(arguments: Arguments, name: str) -> str | None:
    return next((f"`{word.raw}`" for word in arguments.words.get(name, ())), None)


def orca_command(arguments: Arguments) -> tuple[Scalar | None, Scalar | None]:
    group, verb = arguments.values.get("group", ()), arguments.values.get("verb", ())
    return (group[0] if group else None, verb[0] if verb else None)


def orca_ending(spelling: str, group: str, verb: str, arguments: Arguments) -> str:
    values = arguments.values
    match group:
        case "terminal":
            scope = (
                f"terminal {terminal}"
                if (terminal := orca_bound(arguments, "terminal")) is not None
                else f"{'every terminal' if 'all' in values else 'the active terminal'} in worktree {worktree}"
                if (worktree := orca_bound(arguments, "worktree")) is not None
                else "the current tab's terminal"
                if "tab" in values
                else "the current terminal"
            )
            action = f"{'closes' if verb == 'close' else 'stops'} {scope}"
        case "worktree":
            worktree = orca_bound(arguments, "worktree") or "named by its arguments"
            action = f"removes worktree {worktree} and every terminal in it"
        case _:
            action = f"{verb} ends the worker or run {orca_bound(arguments, 'dispatch') or 'named by its arguments'}"
    return f"BLOCKED: `{spelling}` {action}, which ends the agent session living there. {ORCA_READ}"


def help_only(call: Call, values: dict[str, tuple[Scalar | None, ...]]) -> bool:
    if "help" not in values or any(word.value is None for word in call.command.words):
        return False
    path = [arg for arg in call.args if arg not in HELP_FLAGS]
    return len(path) == len(call.args) - 1 and len(path) <= 2 and (not path or path[0] in ORCA_GROUPS)


def closed_terminal(call: Call, arguments: Arguments) -> str | None:
    values = arguments.values
    if (
        call.substituted
        or call.wrappers
        or not arguments.complete
        or not arguments.operands_complete
        or orca_command(arguments) != ("terminal", "close")
        or values.get("rest")
        or not ORCA_CLOSE_SCOPES.isdisjoint(values)
    ):
        return None
    words = arguments.words.get("terminal", ())
    handles = values.get("terminal", ())
    if len(words) != 1 or words[0].value is None or words[0].expandable or not isinstance(handles[0], str):
        return None
    return None if handles[0].startswith("-") else handles[0]


def closes_one_terminal(call: Call) -> bool:
    return call.name == "orca" and closed_terminal(call, ORCA.bind(call)) is not None


def single_close(call: Call, scan: Scan) -> bool:
    return all(other is not call for other in scan.respelled) and sum(map(closes_one_terminal, scan.literal_calls)) == 1


def terminal_close_denied(spelling: str, handle: str, detail: str) -> str:
    return f"BLOCKED: `{spelling}` closes terminal `{clip(handle, 48)}`{detail}. {OWNER_FIX}"


def terminal_close_verdict(call: Call, handle: str, scan: Scan, evt: ToolRewriteEvent) -> str | None:
    deny = partial(terminal_close_denied, clip(call.source.raw, 50), handle)
    if isinstance(answer := evt.annotations.get("owner-authorized"), str):
        match answer_names(answer, handle, evt.cwd):
            case True:
                return None
            case Unreadable(reason):
                return deny(f", and cc-notes answer `{clip(answer, 20)}` was not read ({reason})")
            case _:
                return deny(f", and cc-notes answer `{clip(answer, 20)}` does not name it")
    match scan.facts.terminal_tree(handle):
        case Unreadable(reason):
            return deny(f" ({reason})")
        case (root, *below):
            agent = next((row for row in below if hosts_agent(row)), root if is_agent(root) else None)
            return None if agent is None else deny(f", where {describe(agent)} runs")
        case _:
            return deny(" (its process tree could not be read)")


def orca_ending_verdict(call: Call, scan: Scan, evt: ToolRewriteEvent) -> str | None:
    if call.name != "orca":
        return None
    spelling = spell(call)
    arguments = ORCA.bind(call)
    values = arguments.values
    if help_only(call, values):
        return None
    if (handle := closed_terminal(call, arguments)) is not None and single_close(call, scan):
        return terminal_close_verdict(call, handle, scan, evt)
    found = next(
        (
            (group, verb)
            for group, verbs in ORCA_ENDINGS.items()
            for verb in verbs
            if group in call.args and verb in call.args
        ),
        None,
    )
    if found is not None:
        return orca_ending(spelling, *found, arguments)
    group = values.get("group", ())
    verb = values.get("verb", ())
    if None in group or None in verb:
        return (
            f"BLOCKED: `{spelling}` names its orca group or verb at run time, so it may close a terminal, remove a "
            "worktree, or stop a worker. Spell the group and verb literally."
        )
    if not arguments.operands_complete and (not group or not verb):
        return (
            f"BLOCKED: `{spelling}` hides its orca group or verb behind {hidden_behind(arguments)}, so it may close a "
            "terminal, remove a worktree, or stop a worker. Put the group and verb first."
        )
    match orca_command(arguments):
        case (None, _):
            return None
        case (unknown, _) if unknown not in ORCA_GROUPS:
            return (
                f"BLOCKED: `{spelling}` uses an orca command group the guard does not know, so it cannot rule out an "
                "ending action. Use a group listed by `orca --help`."
            )
        case (str() as group_name, str() as verb_name) if verb_name in ORCA_ENDINGS.get(group_name, ()):
            return orca_ending(spelling, group_name, verb_name, arguments)
        case ("vm", verb_name) if (flag := orca_vm_run_flag(call)) is not None or verb_name != "recipe":
            return (
                f"BLOCKED: `{spelling}` passes `{flag or verb_name}`, which runs a recipe's commands on a VM, and a "
                "recipe may end sessions. Run `orca vm recipe doctor <recipe-id>` without `--provision` or `--connect`."
            )
        case _:
            return None


def orca_vm_run_flag(call: Call) -> str | None:
    return next((arg for arg in call.args if arg.partition("=")[0] in ORCA_VM_RUN_FLAGS), None)


@guard(
    tests={
        guarded(
            command=(
                "for x in term_a term_b; do orca terminal close --terminal $x --json >/dev/null 2>&1 && "
                "echo closed $x; done"
            )
        ): Block(pattern="closes terminal `term_a`"),
        guarded(
            command=(
                "n=0; for x in $(cat /tmp/reap.txt); do orca terminal close --terminal $x --json >/dev/null 2>&1 && "
                "n=$((n+1)); done; echo closed $n; uptime"
            )
        ): Block(pattern="closes terminal"),
        guarded(
            command=(
                'n=0; while read t; do [ -z "$t" ] && continue; orca terminal close --terminal "$t" >/dev/null 2>&1 '
                "&& n=$((n+1)); done < /tmp/close-list.txt"
            )
        ): Block(pattern="closes terminal"),
        guarded(
            command=(
                'orca terminal list --worktree path:$M/$w --json 2>&1 | python3 -c "import json,sys\nfor t in '
                "json.load(sys.stdin)['result']['terminals']: print(t['handle'])\" | while read t; do orca terminal "
                "close --terminal $t 2>&1 | tail -1; done"
            )
        ): Block(pattern="closes terminal"),
        guarded(
            command=(
                f"orca terminal close --terminal {INLINE_OWNER_TERMINAL} --json "
                f"# ccx:owner-authorized={INLINE_OWNER_ANSWER}"
            )
        ): Allow(),
        guarded(
            command=f"orca terminal close --terminal {INLINE_OWNER_TERMINAL} --json # ccx:raw owner-authorized=R740"
        ): Block(pattern="`# ccx:owner-authorized=<cc-notes answer id>`"),
        guarded(
            command=f"orca terminal close --terminal {INLINE_OWNER_TERMINAL} --json # ccx:owner-authorized=r740"
        ): Block(pattern="cc-notes answer `r740` was not read"),
        guarded(
            command=f"orca terminal close --terminal term_agent2 --json # ccx:owner-authorized={INLINE_OWNER_ANSWER}"
        ): Block(pattern="does not name it"),
        guarded(
            command=(
                f"T={INLINE_OWNER_TERMINAL}; orca terminal close --terminal $T "
                f"# ccx:owner-authorized={INLINE_OWNER_ANSWER}"
            )
        ): Block(pattern="leave ending them to the owner"),
        guarded(
            command=(
                f"orca terminal close --terminal {INLINE_OWNER_TERMINAL} --terminal term_agent "
                f"# ccx:owner-authorized={INLINE_OWNER_ANSWER}"
            )
        ): Block(),
        guarded(
            command=(
                "orca terminal close --terminal term_agent; orca terminal close --terminal term_agent "
                f"# ccx:owner-authorized={INLINE_OWNER_ANSWER}"
            )
        ): Block(pattern="leave ending them to the owner"),
        guarded(
            command=f"timeout 5 orca terminal close --terminal term_agent # ccx:owner-authorized={INLINE_OWNER_ANSWER}"
        ): Block(),
        guarded(
            command=f"orca terminal close --terminal term_agent --all # ccx:owner-authorized={INLINE_OWNER_ANSWER}"
        ): Block(),
        guarded(
            command=f"orca orchestration worker-stop --dispatch ctx-1 # ccx:owner-authorized={INLINE_OWNER_ANSWER}"
        ): Block(pattern="worker-stop ends the worker"),
        guarded(command="orca terminal close --terminal term_idle --json"): Allow(),
        guarded(command="orca terminal close --terminal term_agent --json"): Block(
            pattern=r"where pid 16002 \(`claude"
        ),
        guarded(command="orca terminal close --terminal term_shim"): Block(pattern=r"where pid 17002 \(`"),
        guarded(command="orca terminal close --terminal term_gone"): Block(pattern="is not in the process table"),
        guarded(command="orca terminal close --terminal term_nope"): Block(pattern="Orca reports no process"),
        guarded(command="orca terminal close --terminal term_idle; orca terminal close --terminal term_idle"): Block(),
        guarded(command="for x in term_idle; do orca terminal close --terminal $x; done"): Block(
            pattern="leave ending them to the owner"
        ),
        guarded(command="orca terminal close"): Block(pattern="closes the current terminal"),
        guarded(command="orca terminal close --tab"): Block(pattern="the current tab's terminal"),
        guarded(command="orca terminal close --worktree active --all"): Block(
            pattern="every terminal in worktree `active`"
        ),
        guarded(command="orca --json terminal close --terminal t"): Block(),
        guarded(command="orca terminal --json close --terminal=t"): Block(),
        guarded(command="xargs -n1 orca terminal close --terminal"): Block(),
        guarded(command="orca terminal stop --worktree x"): Block(pattern="stops the active terminal in worktree"),
        guarded(command="orca worktree rm --worktree path:/x"): Block(pattern="removes worktree `path:/x`"),
        guarded(command="orca orchestration worker-stop --dispatch ctx-1"): Block(
            pattern="worker-stop ends the worker or run `ctx-1`"
        ),
        guarded(command="orca orchestration worker-release --dispatch ctx-1"): Block(),
        guarded(command="orca $GROUP close --terminal t"): Block(pattern="at run time"),
        guarded(command="for x in term_a term_b; do orca terminal close --terminal $x; done"): Block(
            pattern="closes terminal `term_a`"
        ),
        guarded(command="orca --bogus terminal close"): Block(),
        guarded(command="orca --bogus status"): Block(pattern="hides its orca group or verb behind `--bogus`"),
        guarded(command="orca nuke everything"): Block(pattern="does not know"),
        guarded(
            command=(
                "for d in ctx_c9c192f997b0 ctx_84420bceb40d; do orca orchestration worker-show --dispatch $d --json "
                "2>&1 | jq -c --arg d $d '{d:$d, s:.result.dispatch.status}'; done; "
                "orca orchestration worker-release --help 2>&1 | head -15"
            )
        ): Allow(),
        guarded(command="orca orchestration worker-release --dispatch ctx-1 --help"): Block(),
        guarded(command="orca agent-teams-tmux kill-pane --help -t %2 terminal close"): Block(),
        guarded(command="orca agent-teams-tmux kill-pane --help"): Block(),
        guarded(command="orca orchestration worker-release -- --help"): Block(pattern="worker-release"),
        guarded(command="orca terminal close --help"): Allow(),
        guarded(command="orca terminal close -h"): Allow(),
        guarded(command="orca terminal close --terminal --help"): Block(pattern="closes terminal `--help`"),
        guarded(command="orca --bogus terminal close --help"): Block(),
        guarded(command="read OPT <<<'--terminal'; orca terminal close \"$OPT\" -h --terminal="): Block(),
        guarded(command="orca terminal close -h $T"): Block(),
        guarded(command="orca terminal close --help; orca terminal close"): Block(
            pattern="closes the current terminal"
        ),
        guarded(
            command="orca vm recipe doctor guard-probe --provision --repo-path /Users/yasyf/.claude/scratch/x/vm-probe"
        ): Block(pattern="passes `--provision`"),
        guarded(command="orca vm recipe doctor guard-probe --connect --json"): Block(pattern="passes `--connect`"),
        guarded(command="orca vm recipe doctor guard-probe --provision=true"): Block(
            pattern="passes `--provision=true`"
        ),
        guarded(command="orca vm create guard-probe"): Block(pattern="passes `create`"),
        guarded(command="orca vm recipe doctor cloud-sandbox --repo-path /path/to/repo --json"): Allow(),
        guarded(command="'orca' terminal close"): Block(),
        guarded(
            tool="mcp__runner__exec", tool_input={"command": "orca", "args": ["terminal", "close", "--terminal", "t"]}
        ): Block(pattern="closes terminal `t`"),
        guarded(tool="mcp__x__call", tool_input={"opts": {"cmd": "orca terminal close --terminal term_x"}}): Block(
            pattern="closes terminal `term_x`"
        ),
        guarded(command="orca terminal list --json"): Allow(),
        guarded(command="orca --help"): Allow(),
        guarded(command="orca -h"): Allow(),
        guarded(command="orca terminal --help 2>&1 | head -60"): Allow(),
        guarded(command="orca orchestration --help 2>&1 | head -60"): Allow(),
        guarded(command="orca orchestration --help >/dev/null 2>&1; orca orchestration 2>&1 | head -30"): Allow(),
        guarded(command="orca help terminal 2>&1 | head -80"): Allow(),
        guarded(command="orca orchestration worker-show --dispatch ctx_5a35fc72f713 --json"): Allow(),
        guarded(command="orca orchestration task-list --run run_7715a23a5657 --json"): Allow(),
        guarded(command="orca file open-changed --mode diff"): Allow(),
        guarded(command="orca terminal show --terminal t"): Allow(),
        guarded(command="orca terminal read --terminal t --screen"): Allow(),
        guarded(command="orca status"): Allow(),
        guarded(command="orca --json status"): Allow(),
        guarded(command="orca orchestration check --peek --run r --json"): Allow(),
        guarded(command="orca worktree list --json"): Allow(),
    }
)
def orca_ends_session(evt: ToolRewriteEvent) -> HookResult | None:
    scan = Scan.of(evt)
    return block_first(evt, (orca_ending_verdict(call, scan, evt) for call in scan.literal_calls))


def orca_send_verdict(call: Call) -> str | None:
    if call.name != "orca" or orca_command(arguments := ORCA.bind(call)) != ("terminal", "send"):
        return None
    spelling = spell(call)
    values, words = arguments.values, arguments.words
    text = values.get("text", ())
    if "interrupt" in values or any(
        str(payload).strip().casefold() in END_OF_SESSION for payload in text if payload is not None
    ):
        return (
            f"BLOCKED: `{spelling}` interrupts or exits the agent in another terminal, which ends that session. Send "
            "only ordinary text, and leave interrupts and exits to the owner."
        )
    if None in text:
        return (
            f"BLOCKED: `{spelling}` sends text built at run time "
            f"(`--text {clip(words['text'][text.index(None)].raw, 40)}`), which could be `exit` or a control byte. "
            "Put the literal text in `--text`."
        )
    if "help" in values:
        return None
    if arguments.unread:
        return (
            f"BLOCKED: `{spelling}` passes `{clip(arguments.unread[0].raw, 40)}`, an option the guard does not know, "
            "so it cannot rule out an interrupt. Drop the option."
        )
    for word in words.get("rest", ()):
        flag, _, value = word.raw.partition("=")
        if word.value is None and (flag not in {"--terminal", "--worktree"} or not double_quoted(value)):
            return (
                f"BLOCKED: `{spelling}` passes `{clip(word.raw, 40)}`, an argument named at run time that Orca "
                "may read as `--interrupt` or as the text. Spell it literally, or pass the handle as "
                '`--terminal "$handle"`.'
            )
    loose = next(
        (
            word
            for name, bound in words.items()
            if name != "rest"
            for word in bound
            if word.value is None and not double_quoted(word.raw)
        ),
        None,
    )
    if loose is not None:
        return (
            f"BLOCKED: `{spelling}` names `{clip(loose.raw, 40)}` at run time in a form the shell may split into "
            f'words such as `--interrupt`. Quote it (`"{clip(loose.raw, 40)}"`) or spell it literally.'
        )
    return None


@guard(
    tests={
        guarded(command="orca terminal send --terminal t --interrupt"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text exit --enter"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text /exit --enter"): Block(),
        guarded(command="orca terminal send --terminal t --text /quit --enter"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text logout --enter"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text ^C"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text ^D"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text C-c"): Block(pattern="interrupts or exits"),
        guarded(command="orca terminal send --terminal t --text '\\x03'"): Block(pattern="interrupts or exits"),
        guarded(command='orca terminal send --terminal t --text "$MSG" --enter'): Block(pattern="built at run time"),
        guarded(
            command=(
                "B=\"$(sed -n '1184p' ~/.claude/scratch/release-v3/inbox/orca-desk.md | sed 's/^- //') "
                '-- orca-desk: GO"\n'
                'orca terminal send --terminal term_f8d32118 --text "$B" --enter --wait-submit 15 --json'
            )
        ): Block(pattern="built at run time"),
        guarded(
            command=(
                'w(){ orca terminal send --terminal "$1" --text "$2" --enter 2>&1 | tail -1; }\n'
                "w term_cab63c4c 'orca-desk-4 R508: l08 ALONE linearizes'"
            )
        ): Block(pattern="built at run time"),
        guarded(command="orca terminal send --terminal t --text exit --help"): Block(pattern="interrupts or exits"),
        guarded(command='orca terminal send --terminal t --text "$(cat /tmp/msg.txt)" --enter'): Block(),
        guarded(command="orca terminal send --terminal t --text hi --enter --timeout-ms 3000"): Block(
            pattern="passes `--timeout-ms`"
        ),
        guarded(command='orca terminal send "$X" --text hi --enter'): Block(pattern="may read as `--interrupt`"),
        guarded(command="orca terminal send --terminal $t --text hi --enter"): Block(pattern="may split"),
        guarded(command="orca terminal send --terminal=$t --text hi --enter"): Block(),
        guarded(command='orca terminal send --terminal="$t" --text hi --enter'): Allow(),
        guarded(command="orca terminal send --help 2>&1 | rg -i 'key|interrupt|esc' | head"): Allow(),
        guarded(
            command=(
                "for t in term_ce852041 term_33fabfa8; do orca terminal send --terminal $t --text 'codex -c "
                "model=gpt-6.1-sol' --enter --json | jq -c .ok; done"
            )
        ): Allow(),
        guarded(
            command='for t in term_a term_b; do orca terminal send --terminal "$t" --text "status?" --enter; done'
        ): Allow(),
        guarded(command='orca terminal send --terminal "$ORCA_TERMINAL_HANDLE" --text "note to self" --enter'): Allow(),
        guarded(command='orca terminal send --worktree "$w" --text hi --enter'): Allow(),
        guarded(command='orca terminal send --terminal t --text "ship it" --enter'): Allow(),
    }
)
def orca_send_ends_session(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(orca_send_verdict, Scan.of(evt).literal_calls))


def ends_session_key(key: str) -> bool:
    parts = [KEY_ALIASES.get(part, part) for part in (raw.strip() for raw in key.casefold().split("+"))]
    chords = (
        (frozenset(modifier if part == "cmdorctrl" else part for part in parts) for modifier in ("cmd", "ctrl"))
        if "cmdorctrl" in parts
        else (frozenset(parts),)
    )
    return not END_OF_SESSION_CHORDS.isdisjoint(chords)


def orca_input_verdict(call: Call) -> str | None:
    if call.name != "orca":
        return None
    match orca_command(arguments := ORCA.bind(call)):
        case ("computer", str() as verb) if verb in ORCA_PAYLOADS:
            role = ORCA_PAYLOADS[verb]
        case _:
            return None
    spelling = spell(call)
    values, words = arguments.values.get(role, ()), arguments.words.get(role, ())
    if None in values or f"{role}_stdin" in arguments.values:
        source = f"--{role} {words[values.index(None)].raw}" if None in values else f"--{role}-stdin"
        return (
            f"BLOCKED: `{spelling}` names its {role} at run time (`{clip(source, 40)}`), which could quit, close, "
            f"interrupt, or exit the focused session. Spell the {role} literally."
        )
    if not values and arguments.unread:
        return (
            f"BLOCKED: `{spelling}` hides its {role} behind {hidden_behind(arguments)}, so it may quit, close, "
            f"interrupt, or exit the focused session. Spell the {role} literally."
        )
    match role, next((str(value) for value in values if value is not None), None):
        case ("key", str() as key) if ends_session_key(key):
            return (
                f"BLOCKED: `{spelling}` presses `{clip(key, 40)}`, which quits, closes, interrupts, or exits the "
                "focused session. Press only keys that leave the session running."
            )
        case ("text" | "value", str() as text) if text.strip().casefold() in END_OF_SESSION:
            return (
                f"BLOCKED: `{spelling}` types `{clip(text, 40)}`, which exits the focused terminal. Type only "
                "ordinary text."
            )
        case ("action", str() as action) if END_OF_SESSION_ACTION.search(action) is not None:
            return (
                f"BLOCKED: `{spelling}` performs the `{clip(action, 40)}` action, which quits, closes, or stops its "
                "target. Perform only actions that leave the session running."
            )
        case _:
            return None


@guard(
    tests={
        guarded(command="orca computer hotkey --app Orca --key CmdOrCtrl+Q"): Block(
            pattern="quits, closes, interrupts, or exits the focused session"
        ),
        guarded(command="orca computer hotkey --app Orca --key Cmd+Shift+W"): Block(pattern=r"presses `Cmd\+Shift\+W`"),
        guarded(command="orca computer hotkey --app Orca --key Command+Option+Escape"): Block(),
        guarded(command="orca computer press-key --app Orca --key Ctrl+C"): Block(pattern=r"presses `Ctrl\+C`"),
        guarded(command='orca computer hotkey --app Orca --key "$K"'): Block(pattern="names its key at run time"),
        guarded(command="orca computer hotkey --app Orca --window-index 0 --key Control+D"): Block(),
        guarded(command="orca computer hotkey --app Orca --bogus 1 --key CmdOrCtrl+N"): Block(
            pattern="hides its key behind `--bogus`"
        ),
        guarded(command="orca computer type-text --app Orca --text exit"): Block(pattern="exits the focused terminal"),
        guarded(command="orca computer paste-text --app Orca --text ^C"): Block(pattern="exits the focused terminal"),
        guarded(command="orca computer set-value --app Orca --element-index 3 --value /quit"): Block(),
        guarded(command="orca computer type-text --app Orca --text-stdin < /tmp/payload"): Block(
            pattern=r"names its text at run time \(`--text-stdin`\)"
        ),
        guarded(command='orca computer type-text --app Orca --text "$MSG"'): Block(
            pattern="names its text at run time"
        ),
        guarded(command="orca computer perform-secondary-action --app Orca --element-index 3 --action Quit"): Block(
            pattern="performs the `Quit` action"
        ),
        guarded(
            command="orca computer perform-secondary-action --app Orca --element-index 3 --action AXPress"
        ): Allow(),
        guarded(command="orca computer hotkey --app Orca --key CmdOrCtrl+N"): Allow(),
        guarded(command="orca computer hotkey --app Orca --key Cmd+Shift+N --restore-window"): Allow(),
        guarded(command="orca computer press-key --app Orca --key Escape"): Allow(),
        guarded(command="orca computer press-key --app Orca --key Return"): Allow(),
        guarded(command="orca computer click --app Orca --element-index 12"): Allow(),
        guarded(command='orca computer click --app "$APP" --element-index 3'): Allow(),
        guarded(command='orca computer type-text --app Orca --text "capt-session-guard"'): Allow(),
        guarded(command="orca computer set-value --app Orca --element-index 3 --value feature/x"): Allow(),
        guarded(command="orca computer list-apps"): Allow(),
        guarded(command="orca computer get-app-state --app Orca --json"): Allow(),
        guarded(command="orca computer click --app Terminal --element-index 1"): Allow(),
    }
)
def orca_input_ends_session(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(orca_input_verdict, Scan.of(evt).literal_calls))


def launchctl_verdict(call: Call) -> str | None:
    if call.name != "launchctl":
        return None
    spelling = spell(call)
    arguments = LAUNCHCTL.bind(call)
    match arguments.values.get("verb", ()):
        case (None,):
            return (
                f"BLOCKED: `{spelling}` names its launchctl verb at run time, so it may stop, unload, or reboot a "
                "service that hosts sessions. Spell the verb literally."
            )
        case (str() as verb,) if verb in LAUNCHCTL_ENDINGS:
            return (
                f"BLOCKED: `{spelling}` stops, unloads, restarts, or reboots a launchd service or domain that may host "
                "sessions. Inspect it with `launchctl list` or `launchctl print <target>` instead."
            )
        case () if not arguments.operands_complete:
            return (
                f"BLOCKED: `{spelling}` hides its launchctl verb behind {hidden_behind(arguments)}, so it may stop or "
                "unload a service that hosts sessions. Put the verb first."
            )
        case _:
            return None


@guard(
    tests={
        guarded(command="launchctl reboot system"): Block(pattern="launchctl reboot"),
        guarded(command="launchctl bootout gui/501/com.example.orca-serve"): Block(
            pattern="`launchctl bootout gui/501/com.example.orca-serve`"
        ),
        guarded(command="launchctl kickstart -k system/com.example.host"): Block(),
        guarded(command="launchctl $verb gui/501"): Block(pattern="at run time"),
        guarded(command="launchctl -q bootout gui/501/com.example.orca-serve"): Block(
            pattern="hides its launchctl verb behind `-q`"
        ),
        guarded(command="launchctl list"): Allow(),
        guarded(command="launchctl print gui/501"): Allow(),
    }
)
def launchctl_stops_service(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(launchctl_verdict, Scan.of(evt).literal_calls))


def osascript_verdict(call: Call) -> str | None:
    if call.name != "osascript":
        return None
    spelling = spell(call)
    if (scripts := applescripts(call)) is None:
        return (
            f"BLOCKED: `{spelling}` runs an AppleScript statement built at run time, which may quit Orca, log out, "
            "restart, or sleep the Mac. Spell the statement literally."
        )
    if any(APPLESCRIPT_ENDING.search(script) for script in scripts):
        return (
            f"BLOCKED: `{spelling}` quits an application, logs out, restarts, shuts down, or sleeps the Mac, which "
            "ends every session on it. Ask the owner to run it."
        )
    if any(script is None for script in shell_scripts(scripts)):
        return (
            f"BLOCKED: `{spelling}` runs a `do shell script` that AppleScript builds at run time, so the guard cannot "
            "see what runs. Spell the shell command literally."
        )
    return None


@guard(
    tests={
        guarded(command="osascript -e 'tell application \"Orca\" to quit'"): Block(pattern="quits an application"),
        guarded(command="osascript -e 'tell application \"System Events\" to restart'"): Block(),
        guarded(
            command='osascript -e \'tell application "System Events" to keystroke "q" using command down\''
        ): Block(),
        guarded(command='osascript -e "$S"'): Block(pattern="built at run time"),
        guarded(command="osascript <<'EOF'\ntell application \"Orca\" to quit\nEOF"): Block(),
        guarded(command="osascript -e 'do shell script theCommand'"): Block(pattern="AppleScript builds at run time"),
        guarded(command="osascript -e 'do shell script \"ls -la\"'"): Allow(),
        guarded(command="osascript -e 'display notification \"done\"'"): Allow(),
    }
)
def osascript_ends_session(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(osascript_verdict, Scan.of(evt).literal_calls))


def softwareupdate_verdict(call: Call) -> str | None:
    if call.name != "softwareupdate":
        return None
    arguments = SOFTWAREUPDATE.bind(call)
    if "mutate" not in arguments.values and arguments.complete:
        return None
    return (
        f"BLOCKED: `{spell(call)}` installs or stages a system update, which restarts the Mac and ends every session "
        "on it. Use the read-only `softwareupdate -l` or `softwareupdate --history` instead."
    )


@guard(
    tests={
        guarded(command="softwareupdate -i -a -R"): Block(pattern="installs or stages"),
        guarded(command="softwareupdate --install --all --restart"): Block(),
        guarded(command="softwareupdate --force --install --all"): Block(pattern="installs or stages"),
        guarded(command="softwareupdate -l"): Allow(),
        guarded(command="softwareupdate --history"): Allow(),
    }
)
def softwareupdate_restarts_mac(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(softwareupdate_verdict, Scan.of(evt).literal_calls))


def pmset_verdict(call: Call) -> str | None:
    if call.name != "pmset":
        return None
    spelling = spell(call)
    arguments = PMSET.bind(call)
    rest = arguments.values.get("rest", ())
    match arguments.values.get("verb", ()):
        case (None,):
            return (
                f"BLOCKED: `{spelling}` names its pmset verb at run time, so it may sleep or restart the Mac. Spell "
                "the verb literally."
            )
        case (str() as verb,) if verb in PMSET_ENDINGS:
            return (
                f"BLOCKED: `{spelling}` sleeps, halts, or restarts the Mac, which suspends or ends every session on "
                "it. Read power state with `pmset -g` instead."
            )
        case ("schedule" | "repeat",) if None in rest or any(str(word) in PMSET_SCHEDULES for word in rest):
            return (
                f"BLOCKED: `{spelling}` schedules a shutdown, restart, sleep, or power-off, which ends every session "
                "on the Mac. Read the schedule with `pmset -g sched` instead."
            )
        case () if not arguments.operands_complete:
            return (
                f"BLOCKED: `{spelling}` hides its pmset verb behind {hidden_behind(arguments)}, so it may sleep or "
                "restart the Mac. Put the verb first."
            )
        case _:
            return None


@guard(
    tests={
        guarded(command="pmset sleepnow"): Block(pattern="pmset sleepnow"),
        guarded(command="pmset schedule shutdown '09/30/26 23:00:00'"): Block(),
        guarded(command="pmset repeat shutdown MTWRFSU 23:00:00"): Block(pattern="schedules a shutdown"),
        guarded(command="pmset -z sleepnow"): Block(pattern="hides its pmset verb behind `-z`"),
        guarded(command="pmset repeat cancel"): Allow(),
        guarded(command="pmset -g"): Allow(),
        guarded(command="pmset -g sched"): Allow(),
    }
)
def pmset_sleeps_mac(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(pmset_verdict, Scan.of(evt).literal_calls))


def tmux_verdict(call: Call) -> str | None:
    if call.name != "tmux":
        return None
    spelling = spell(call)
    arguments = TMUX.bind(call)
    match arguments.values.get("verb", ()):
        case (None,):
            return (
                f"BLOCKED: `{spelling}` names its tmux verb at run time, so it may kill an agent's session. Spell the "
                "verb literally."
            )
        case (str() as verb,) if verb in TMUX_ENDINGS:
            return (
                f"BLOCKED: `{spelling}` kills a tmux server, session, window, or pane, ending the agent session "
                "attached there. Inspect with `tmux ls` or `tmux list-panes` instead."
            )
        case () if not arguments.operands_complete:
            return (
                f"BLOCKED: `{spelling}` hides its tmux verb behind {hidden_behind(arguments)}, so it may kill an "
                "agent's session. Put the verb first."
            )
        case _:
            return None


@guard(
    tests={
        guarded(command="tmux kill-server"): Block(pattern="tmux kill-server"),
        guarded(command="tmux -L work kill-session -t main"): Block(pattern="`tmux -L work kill-session -t main`"),
        guarded(command="tmux -N kill-server"): Block(pattern="hides its tmux verb behind `-N`"),
        guarded(command="tmux ls"): Allow(),
        guarded(command="tmux send-keys -t main ls Enter"): Allow(),
    }
)
def tmux_kills_session(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(tmux_verdict, Scan.of(evt).literal_calls))


def guarded_program_in(args: tuple[str, ...]) -> str | None:
    return next(
        (
            name
            for arg in args
            for token in ARG_TOKEN_BREAK.split(arg)
            if (name := PurePath(QUOTING_CHARS.sub("", token)).name.casefold()) in GUARDED_PROGRAMS
        ),
        None,
    )


def hidden_target(words: tuple[Word, ...]) -> Word | None:
    return next((word for word in words if word.value is None), None)


def unquoted_expansion(word: Word) -> bool:
    parts = segments(word.raw)
    return parts is None or any(isinstance(part, Ref) and not part.quoted for part in parts)


def launcher_verdict(call: Call) -> str | None:
    if call.name not in LAUNCHERS:
        return None
    if (hidden := hidden_target(call.command.words[1:])) is not None:
        return (
            f"BLOCKED: `{spell(call)}` runs a command named at run time (`{clip(hidden.raw, 40)}`) through "
            f"`{call.name}`, which hides its target from the guard. Spell the command name literally."
        )
    if (inner := guarded_program_in(call.args)) is None:
        return None
    return (
        f"BLOCKED: `{spell(call)}` runs `{inner}` through `{call.name}`, which hides its target from the guard. Run "
        f"`{inner}` directly as its own command."
    )


@guard(
    tests={
        guarded(command="caffeinate kill 14575"): Block(pattern="hides its target from the guard"),
        guarded(command="setsid kill 14575"): Block(),
        guarded(command="builtin kill 14575"): Block(),
        guarded(command="su root -c 'kill 14575'"): Block(),
        guarded(command="caffeinate sh -c 'x;kill 14575'"): Block(pattern="runs `kill` through `caffeinate`"),
        guarded(command="watch 'true;kill 14575'"): Block(),
        guarded(command='watch -n1 "pgrep x|xargs kill"'): Block(),
        guarded(command="caffeinate /bin/kill 14575"): Block(),
        guarded(command="caffeinate PKILL -f claude"): Block(pattern="runs `pkill`"),
        guarded(command="watch -n1 PKILL -x sleep"): Block(),
        guarded(command="setsid SHUTDOWN -h now"): Block(),
        guarded(command="stdbuf -oL tail -f /tmp/orca.log"): Allow(),
        guarded(command="caffeinate -i make kill-target"): Allow(),
        guarded(command="caffeinate -i ./scripts/reboot-check.sh"): Allow(),
        guarded(command="arch -arm64 make test-kill"): Allow(),
        guarded(command="caffeinate -i make -C /tmp/ws/x test"): Allow(),
        guarded(command="caffeinate -i make test"): Allow(),
        guarded(command='read -r K <<< kill; caffeinate "$K" 14575'): Block(pattern="named at run time"),
        guarded(command="read N; caffeinate -i make -j$N; ls ~/.orca"): Block(pattern="named at run time"),
        guarded(command='PWD="i /bin/kill 14575"; caffeinate -$PWD'): Block(pattern="named at run time"),
        guarded(command="N=4; caffeinate -i make -j$N; ls ~/.orca"): Allow(),
        guarded(command="X=echo; caffeinate $X 14575; ls ~/.orca"): Allow(),
    }
)
def launcher_hides_target(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(launcher_verdict, Scan.of(evt).literal_calls))


def find_exec_verdict(call: Call) -> str | None:
    if call.name != "find":
        return None
    if (
        loose := next(
            (word for word in call.command.words[1:] if word.value is None and unquoted_expansion(word)), None
        )
    ) is not None:
        return (
            f"BLOCKED: `{spell(call)}` expands `{clip(loose.raw, 40)}` unquoted inside a find expression, where it "
            "can add an `-exec` the guard cannot see. Quote it or spell it literally."
        )
    start = next((index for index, arg in enumerate(call.args) if arg in FIND_EXEC), None)
    if start is None:
        return None
    if (program := hidden_target(call.command.words[start + 2 : start + 3])) is not None:
        return (
            f"BLOCKED: `{spell(call)}` runs a command named at run time (`{clip(program.raw, 40)}`) through "
            "`find -exec`, on targets the guard cannot see. Spell the command name literally."
        )
    if (inner := guarded_program_in(call.args[start + 1 :])) is None:
        return None
    return (
        f"BLOCKED: `{spell(call)}` runs `{inner}` once per matched file through `find -exec`, on targets the guard "
        f"cannot see. Run `{inner}` directly against literal pids you verified."
    )


@guard(
    tests={
        guarded(command="find /tmp -name '*.pid' -exec kill {} +"): Block(pattern="find -exec"),
        guarded(command="find /tmp -name '*.pid' -exec KILL {} +"): Block(pattern="find -exec"),
        guarded(command="find . -exec sh -c 'kill $1' _ {} \\;"): Block(),
        guarded(command="find . -name '*kill*' -exec cat {} +"): Allow(),
        guarded(command="find ./orca -name x -exec cat {} +"): Allow(),
        guarded(command="read -r K <<< kill; find /tmp -exec \"$K\" 14575 ';'"): Block(pattern="named at run time"),
        guarded(command="read N; find /tmp -maxdepth 1 -exec echo \"$N\" {} ';'; ls ~/.orca"): Allow(),
        guarded(command="read N; find /tmp -maxdepth 1 -exec echo $N {} ';'; ls ~/.orca"): Block(
            pattern="unquoted inside a find expression"
        ),
        guarded(
            command='read N <<<"; -exec /bin/kill 14575 ; -exec echo"; find /tmp -maxdepth 1 -exec echo $N {} ";"'
        ): Block(pattern="unquoted inside a find expression"),
        guarded(command="read D; find $D -name x"): Block(pattern="unquoted inside a find expression"),
        guarded(command="find . -name '*.pyc' -delete"): Allow(),
    }
)
def find_exec_hides_target(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(find_exec_verdict, Scan.of(evt).literal_calls))


def trap_verdict(call: Call) -> str | None:
    if call.name != "trap" or (action := first_operand(call)) is None or action.value is not None:
        return None
    return (
        f"BLOCKED: `{spell(call)}` installs a trap action built at run time, so the guard cannot see what runs when "
        "it fires. Spell the action literally."
    )


@guard(
    tests={
        guarded(command='trap "$ACT" EXIT; kill -l'): Block(pattern="trap action built at run time"),
        guarded(command="trap 'rm -f /tmp/lock' EXIT; kill -l"): Allow(),
    }
)
def trap_hides_action(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(trap_verdict, Scan.of(evt).literal_calls))


def source_verdict(call: Call) -> str | None:
    if call.name not in {"source", ""} or call.command.words[0].value not in {"source", "."}:
        return None
    script = first_operand(call)
    if script is not None and script.value is not None and script.value not in STDIN_SCRIPTS:
        return None
    return (
        f"BLOCKED: `{spell(call)}` sources commands the guard cannot see from stdin, a process substitution, or a "
        "file named at run time. Run the commands directly."
    )


@guard(
    tests={
        guarded(command="source <(echo pkill -x sleep)"): Block(pattern="sources commands the guard cannot see"),
        guarded(command="echo kill 14575 | . /dev/stdin"): Block(pattern="sources commands"),
        guarded(command="source ./env.sh; kill -l"): Allow(),
    }
)
def source_hides_commands(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(source_verdict, Scan.of(evt).literal_calls))


def too_deep(spelling: str) -> str:
    return (
        f"BLOCKED: `{spelling}` nests shells deeper than the guard can expand, so the innermost command is unchecked. "
        "Flatten the nesting into one shell."
    )


def eval_verdict(call: Call, spelling: str) -> str | None:
    words = call.command.words[1:]
    if call.substituted or any(word.value is None for word in words):
        return (
            f"BLOCKED: `{spelling}` evaluates text built at run time, so the guard cannot see what runs. Run the "
            "command directly."
        )
    return too_deep(spelling) if words and call.occurrence.nesting == PAYLOAD_DEPTH_LIMIT else None


def short_flag(value: str | None, letter: str) -> bool:
    return value is not None and value.startswith("-") and not value.startswith("--") and letter in value[1:]


def shell_verdict(call: Call) -> str | None:
    if call.name not in SHELLS and call.name != "eval":
        return None
    spelling = spell(call)
    if call.name == "eval":
        return eval_verdict(call, spelling)
    words = call.command.words[1:]
    flagged = next((index for index, word in enumerate(words) if short_flag(word.value, "c")), None)
    if flagged is None:
        script = first_operand(call)
        if script is not None and script.value is not None and not any(short_flag(word.value, "s") for word in words):
            return None
        return (
            f"BLOCKED: `{spelling}` runs a shell on commands the guard cannot see from stdin, a heredoc, a "
            "here-string, or a script named at run time. Run the commands directly."
        )
    payload = next(
        (word for word in words[flagged + 1 :] if word.value is None or not word.value.startswith("-")), None
    )
    if payload is None:
        return None
    if payload.value is None:
        return (
            f"BLOCKED: `{spelling}` runs a shell payload built at run time, so the guard cannot see what runs. Run the "
            "command directly."
        )
    return too_deep(spelling) if call.occurrence.nesting == PAYLOAD_DEPTH_LIMIT else None


@guard(
    tests={
        guarded(command='bash -c "$CMD"; kill -l'): Block(pattern="shell payload built at run time"),
        guarded(command='eval "$(echo pkill sleep)"'): Block(pattern="evaluates text built at run time"),
        guarded(command=nested(4, "pkill -x sleep")): Block(pattern="nests shells deeper"),
        guarded(command=nested(3, "eval 'pkill -f claude'")): Block(pattern="nests shells deeper"),
        guarded(command=nested(4, "kill -9 14575", wrapper="eval")): Block(pattern="nests shells deeper"),
        guarded(command="echo pkill -x sleep | sh"): Block(pattern="commands the guard cannot see"),
        guarded(command="bash <<< 'pkill -x sleep'"): Block(pattern="commands the guard cannot see"),
        guarded(command="sh <<'EOF'\npkill -x sleep\nEOF"): Block(pattern="commands the guard cannot see"),
        guarded(command="printf 'kill 14575' | zsh"): Block(),
        guarded(command="bash -s <<< 'kill 14575'"): Block(),
        guarded(command='bash "$SCRIPT"; kill -l'): Block(pattern="commands the guard cannot see"),
        guarded(command="bash ./run-tests.sh; kill -l"): Allow(),
        guarded(command=nested(3, "pkill -x sleep")): Allow(),
    }
)
def shell_hides_commands(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(shell_verdict, Scan.of(evt).literal_calls))


def unparsed_message(unparsed: Unparsed) -> str:
    return (
        f"BLOCKED: {unparsed.source} names `{unparsed.program}` but is not shell the guard can parse, so it cannot "
        "verify what runs. Rewrite it as plain shell commands."
    )


@guard(
    tests={
        guarded(tool="mcp__x__exec", tool_input={"command": "(" * 2000 + "kill 1" + ")" * 2000}): Block(
            pattern="not shell the guard can parse"
        ),
        guarded(command="# kill 14575 later"): Block(pattern="this `Bash` payload names `kill`"),
        guarded(tool="mcp__jupyter__run", tool_input={"code": "import os\nos.kill(4242, 0)"}): Block(
            pattern="this `mcp__jupyter__run` payload names `kill`"
        ),
        guarded(tool="mcp__jupyter__run", tool_input={"code": "print('watch this ('"}): Allow(),
        guarded(
            tool="Workflow",
            tool_input={
                "script": (
                    "const results = await parallel(files.map((f) => () => agent({prompt: `find bugs in ${f}`})));\n"
                    "if (results.ok) { return results; }"
                )
            },
        ): Allow(),
        guarded(tool="Skill", tool_input={"skill": "code-review", "args": "don't kill the dev server"}): Allow(),
        guarded(command="git status"): Allow(),
    }
)
def unparsed_payload(evt: ToolRewriteEvent) -> HookResult | None:
    return block_first(evt, map(unparsed_message, Scan.of(evt).unparsed))
