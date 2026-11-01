from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, CommandMatches, CustomCondition, Event, Input, Or, Tool, hook, search_policy
from captain_hook.app import registered_pack_roots
from captain_hook.search_policy import (
    BROAD_ROOTS as BROAD_ROOTS,
)
from captain_hook.search_policy import (
    FIND as FIND,
)
from captain_hook.search_policy import (
    GIT_GREP as GIT_GREP,
)
from captain_hook.search_policy import (
    GIT_GREP_GROUPS as GIT_GREP_GROUPS,
)
from captain_hook.search_policy import (
    GIT_LOCATION_OPTIONS as GIT_LOCATION_OPTIONS,
)
from captain_hook.search_policy import (
    GIT_LOCATION_VARIABLES as GIT_LOCATION_VARIABLES,
)
from captain_hook.search_policy import (
    GREP as GREP,
)
from captain_hook.search_policy import (
    HOME_POOL_LEVELS as HOME_POOL_LEVELS,
)
from captain_hook.search_policy import (
    INFO_ROLES as INFO_ROLES,
)
from captain_hook.search_policy import (
    PATTERN_ROLES as PATTERN_ROLES,
)
from captain_hook.search_policy import (
    POOL_MEMBERS as POOL_MEMBERS,
)
from captain_hook.search_policy import (
    POOL_ROOTS as POOL_ROOTS,
)
from captain_hook.search_policy import (
    POOLS as POOLS,
)
from captain_hook.search_policy import (
    RECURSE_ACTIONS as RECURSE_ACTIONS,
)
from captain_hook.search_policy import (
    RECURSIVE_ROLES as RECURSIVE_ROLES,
)
from captain_hook.search_policy import (
    RG as RG,
)
from captain_hook.search_policy import (
    RG_OUTPUT_ROLES as RG_OUTPUT_ROLES,
)
from captain_hook.search_policy import (
    SHORT_MAGIC as SHORT_MAGIC,
)
from captain_hook.search_policy import (
    STDIN_REDIRECTS as STDIN_REDIRECTS,
)
from captain_hook.search_policy import (
    SYSTEM_ROOTS as SYSTEM_ROOTS,
)
from captain_hook.search_policy import (
    TEMP_ROOTS as TEMP_ROOTS,
)
from captain_hook.search_policy import (
    UGREP as UGREP,
)
from captain_hook.search_policy import (
    CommandSchema as CommandSchema,
)
from captain_hook.search_policy import (
    Operand as Operand,
)
from captain_hook.search_policy import (
    Option as Option,
)
from captain_hook.search_policy import (
    OptionIs as OptionIs,
)
from captain_hook.search_policy import (
    PathMatches as PathMatches,
)
from captain_hook.search_policy import (
    Target as Target,
)
from captain_hook.search_policy import (
    Targets as Targets,
)
from captain_hook.search_policy import (
    assigned_names as assigned_names,
)
from captain_hook.search_policy import (
    at_home as at_home,
)
from captain_hook.search_policy import (
    bounded as bounded,
)
from captain_hook.search_policy import (
    canonical as canonical,
)
from captain_hook.search_policy import (
    common_git_dir as common_git_dir,
)
from captain_hook.search_policy import (
    dequote as dequote,
)
from captain_hook.search_policy import (
    encloses_home_or_pool as encloses_home_or_pool,
)
from captain_hook.search_policy import (
    encloses_roots as encloses_roots,
)
from captain_hook.search_policy import (
    find_searches_broadly as find_searches_broadly,
)
from captain_hook.search_policy import (
    git_cwd as git_cwd,
)
from captain_hook.search_policy import (
    git_dir as git_dir,
)
from captain_hook.search_policy import (
    git_grep_operands as git_grep_operands,
)
from captain_hook.search_policy import (
    git_location as git_location,
)
from captain_hook.search_policy import (
    git_relocated as git_relocated,
)
from captain_hook.search_policy import (
    glob_prefix as glob_prefix,
)
from captain_hook.search_policy import (
    grep_bounded as grep_bounded,
)
from captain_hook.search_policy import (
    grep_searches_broadly as grep_searches_broadly,
)
from captain_hook.search_policy import (
    host_calls as host_calls,
)
from captain_hook.search_policy import (
    informational as informational,
)
from captain_hook.search_policy import (
    is_repo_root as is_repo_root,
)
from captain_hook.search_policy import (
    listed as listed,
)
from captain_hook.search_policy import (
    lists_files as lists_files,
)
from captain_hook.search_policy import (
    lists_home_or_pool as lists_home_or_pool,
)
from captain_hook.search_policy import (
    named_roots as named_roots,
)
from captain_hook.search_policy import (
    names_files as names_files,
)
from captain_hook.search_policy import (
    names_path as names_path,
)
from captain_hook.search_policy import (
    narrows as narrows,
)
from captain_hook.search_policy import (
    pathspec_magic as pathspec_magic,
)
from captain_hook.search_policy import (
    pool_anchors as pool_anchors,
)
from captain_hook.search_policy import (
    pool_member as pool_member,
)
from captain_hook.search_policy import (
    positive as positive,
)
from captain_hook.search_policy import (
    reads_stdin as reads_stdin,
)
from captain_hook.search_policy import (
    recursive as recursive,
)
from captain_hook.search_policy import (
    rg_bounded as rg_bounded,
)
from captain_hook.search_policy import (
    spans_checkouts as spans_checkouts,
)
from captain_hook.search_policy import (
    top_broad as top_broad,
)
from captain_hook.search_policy import (
    ugrep_bounded as ugrep_bounded,
)
from captain_hook.search_policy import (
    written as written,
)

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent

broad = partial(search_policy.broad, pack_roots=registered_pack_roots)
git_grep_searches_broadly = partial(search_policy.git_grep_searches_broadly, pack_roots=registered_pack_roots)
known_search_roots = partial(search_policy.known_search_roots, pack_roots=registered_pack_roots)
pathspec_broad = partial(search_policy.pathspec_broad, pack_roots=registered_pack_roots)
rg_searches_broadly = partial(search_policy.rg_searches_broadly, pack_roots=registered_pack_roots)
searches_broadly = partial(search_policy.searches_broadly, pack_roots=registered_pack_roots)
ugrep_searches_broadly = partial(search_policy.ugrep_searches_broadly, pack_roots=registered_pack_roots)

SEARCH_RULE, FILES_RULE, FIND_RULE = search_policy.shell_search_rules(pack_roots=registered_pack_roots)


class BroadMCPGrep(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        return search_policy.mcp_searches_broadly(evt.input.raw, evt.cwd, pack_roots=registered_pack_roots)


hook(
    Event.PreToolUse,
    only_if=[Tool("ccx_code_grep"), BroadMCPGrep()],
    message=(search_policy.MCP_GREP_MESSAGE),
    block=True,
    max_fires=None,
    tests={
        Input(tool="mcp__cc_context__ccx_code_grep", tool_input={"text": "needle", "paths": ["/"]}): Block(),
        Input(
            tool="mcp__plugin_cc-context_cc-context__ccx_code_grep",
            tool_input={"text": "needle", "repo": "/"},
        ): Block(),
        Input(
            tool="mcp__cc_context__ccx_code_grep",
            tool_input={"text": "needle", "repo": "/repo", "paths": ["src/specific.py"]},
        ): Allow(),
        Input(
            tool="mcp__cc_context__ccx_code_grep", tool_input={"text": "needle", "globs": ["*.py"]}, cwd="/repo"
        ): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        Or(
            *(
                CommandMatches(pattern.schema, only_if=pattern.only_if, skip_if=pattern.skip_if)
                for pattern in SEARCH_RULE.patterns
            )
        ),
    ],
    message=(SEARCH_RULE.message),
    block=True,
    max_fires=None,
    tests={
        Input(command="ugrep -rn needle /Users/yasyf/.claude/scratch/release-v3"): Block(
            pattern="reads every file under it"
        ),
        Input(command="ugrep -rl needle ~/.claude/scratch/release-v3"): Block(),
        Input(
            command='grep -rl "backup in cloud storage" ~/.claude/scratch/durable-agents-rebuild '
            "2>/dev/null | grep -v node_modules | head -3"
        ): Block(),
        Input(command="grep -r -e needle /"): Block(),
        Input(command="grep -r -f patterns.txt ~"): Block(),
        Input(command="grep -r --regexp=needle /"): Block(),
        Input(command="sudo /usr/bin/grep -R needle /Users/alice"): Block(),
        Input(command="env X=1 timeout 10 ugrep -rn needle /Users/alice"): Block(),
        Input(command="sh -c 'cd / && ugrep -rn needle .'"): Block(),
        Input(command="grep -rn needle /Users/alice | head -1"): Block(),
        Input(command=r'grep -rn "CCI_RELAY_URL\s*=" infra/', cwd="/repo"): Block(),
        Input(command="grep -rIn needle infra/", cwd="/repo"): Block(),
        Input(command="grep -rn --include='*' needle infra/", cwd="/repo"): Block(),
        Input(command="grep -r -e needle internal", cwd="/repo"): Block(),
        Input(command="grep -rn needle internal/f*", cwd="/repo"): Block(),
        Input(command="grep -rn needle - infra/", cwd="/repo"): Block(),
        Input(command="grep -Rni needle ~"): Block(),
        Input(command="grep -d recurse needle /"): Block(),
        Input(command="grep -rn -- needle /tmp/scratch-lane"): Block(),
        Input(command="rg needle /"): Block(),
        Input(command="rg --max-count 1 needle /"): Block(),
        Input(command="rg -m1 needle ~ | head -1"): Block(),
        Input(command="rg -d 1 needle ~"): Block(),
        Input(command="rg -g'*.py' -g'**' needle ~"): Block(),
        Input(command="rg -g'!*.md' needle ~"): Block(),
        Input(command="rg -tall needle ~"): Block(),
        Input(command="rg --type-add 'mine:*' -tmine needle ~"): Block(),
        Input(command="rg -e needle -e other ~"): Block(),
        Input(command="rg needle ~/.claude/worktrees/captain-hook"): Block(),
        Input(command="rg needle ~/repos/claude/worktrees"): Block(),
        Input(command="rg needle ~/repos/claude/worktrees/captain-hook"): Block(),
        Input(command="rg needle ~/repos/orca/workspaces"): Block(),
        Input(command="rg needle ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17"): Block(),
        Input(command="rg needle ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17/captain-hook"): Block(),
        Input(command="ugrep -rn needle ~/.orca/workspaces/captain-hook"): Block(),
        Input(command="rg needle src; rg needle /", cwd="/repo"): Block(),
        Input(command='rg needle "$ROOT"'): Block(),
        Input(command="rg needle"): Block(),
        Input(command="grep -r --include='*' needle ~"): Block(),
        Input(command="grep -r --exclude-dir=node_modules needle ~"): Block(),
        Input(command="ugrep -2 needle /"): Block(),
        Input(command="ugrep -r -g'*.py,*' needle ~"): Block(),
        Input(command="ugrep -r -M python needle ~"): Block(),
        Input(command="ugrep -r --and needle --not other ~"): Block(),
        Input(command="ugrep -r -g'*.py' --all needle ~"): Block(),
        Input(command="ugrep --include-fs -r needle ~"): Block(),
        Input(command="ugrep --max-depth=3 needle /"): Block(),
        Input(command="ugrep -r --match /"): Block(),
        Input(command="printf needle | rg needle </dev/null"): Block(),
        Input(command="git grep needle"): Block(),
        Input(command="git -C / grep needle"): Block(),
        Input(command="git -C ~ grep -n needle"): Block(),
        Input(command="git -C ~/.claude/worktrees/captain-hook grep needle"): Block(),
        Input(command='git -C "$WORKTREE" grep needle'): Block(),
        Input(command="git -c core.quotepath=off -C ~ grep -c needle"): Block(),
        Input(command="sh -c 'cd ~ && git grep needle'"): Block(),
        Input(command="git grep -n -e needle -- .", cwd="/Users/alice"): Block(),
        Input(command="git grep needle -- ':!vendor'", cwd="/Users/alice"): Block(),
        Input(command="git grep needle HEAD -- .", cwd="/Users/alice"): Block(),
        Input(command="git grep needle HEAD", cwd="/Users/alice"): Block(),
        Input(command="git grep --bogus-flag needle -- src", cwd="/Users/alice"): Block(),
        Input(command="git grep needle -- :/"): Block(),
        Input(command="git grep -- needle", cwd="/Users/alice"): Block(),
        Input(command="SPEC=':/'; git grep needle -- \"$SPEC\""): Block(),
        Input(command="git grep needle -- ':(top,glob)**/**'"): Block(),
        Input(command="git --namespace -Cinternal grep needle", cwd="/Users/alice"): Block(),
        Input(command="git --git-dir=/srv/demo/.git grep --cached needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="git --git-dir /srv/demo/.git grep needle -- .", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="GIT_DIR=/srv/demo/.git git grep --cached needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="env GIT_DIR=/srv/demo/.git git grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="export GIT_DIR=/srv/demo/.git; git grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="git --work-tree=/srv/demo grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(
            command="git --git-dir=/srv/demo/.git grep --cached needle -- internal/..",
            cwd="/srv/demo/internal/focused",
        ): Block(),
        Input(command="git grep needle -- src/..", cwd="/Users/alice"): Block(),
        Input(command="env 'GIT_DIR=/srv/demo/.git' git grep needle", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="GIT_DIR=/srv/demo/.git sh -c 'git grep needle'", cwd="/srv/demo/internal/focused"): Block(),
        Input(command="git grep -nC3 needle", cwd="/Users/alice"): Block(),
        Input(command="git grep -nC 3 needle -- .", cwd="/Users/alice"): Block(),
        Input(command="git grep -nOvim needle -- src", cwd="/Users/alice"): Block(),
        Input(command="grep -n needle explicit.log", cwd="/repo"): Allow(),
        Input(command="grep -rn needle -", cwd="/repo"): Allow(),
        Input(command="rg needle config.toml tests/test_specific.py", cwd="/repo"): Allow(),
        Input(command="ugrep -rn needle ~/.claude/scratch/release-v3/specific-lane"): Allow(),
        Input(command="grep -rn --include='*.py' needle ~"): Allow(),
        Input(command="grep -rIn --include='*.py' needle infra/", cwd="/repo"): Allow(),
        Input(command="grep -rn --include='*.py' --include='*.json' needle ~"): Allow(),
        Input(command="rg -g'*.json' needle ~"): Allow(),
        Input(command="rg -tpy needle ~"): Allow(),
        Input(command="ugrep -r -tpy,json needle ~"): Allow(),
        Input(command="ugrep -g 'src/*.py' needle ~"): Allow(),
        Input(command="grep -rn needle *.py", cwd="/"): Block(),
        Input(command="grep -rn --include='*.py' needle internal/f*", cwd="/repo"): Allow(),
        Input(command="cat explicit.log | rg needle"): Allow(),
        Input(command="rg needle <explicit.log"): Allow(),
        Input(command="cat explicit.log | ugrep needle"): Allow(),
        Input(command="rg --files /tmp/scratch-lane"): Allow(),
        Input(command="rg --type-list"): Allow(),
        Input(command="rg needle / --help"): Allow(),
        Input(command="echo 'ugrep -rn needle /'"): Allow(),
        Input(command="rg '/' src", cwd="/repo"): Allow(),
        Input(command="rg -e / src", cwd="/repo"): Allow(),
        Input(command="grep -rn -- '~' -", cwd="/repo"): Allow(),
        Input(command="grep -rn -- '~' src", cwd="/repo"): Block(),
        Input(command="ugrep needle /"): Allow(),
        Input(command="git grep needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git grep -c needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git -C /srv/project/internal grep needle"): Allow(),
        Input(command="git -C /srv/demo/internal/focused grep needle"): Allow(),
        Input(
            command="git --git-dir=/srv/demo/.git grep --cached needle -- internal/focused",
            cwd="/srv/demo/internal/focused",
        ): Allow(),
        Input(command="git grep -nC3 needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git grep needle -- src/../lib", cwd="/Users/alice"): Allow(),
        Input(command="git grep -nC 3 needle -- src", cwd="/Users/alice"): Allow(),
        Input(command="git grep needle -- '*.py'"): Allow(),
        Input(command="git grep -e needle -- ':(glob)**/*.py'"): Allow(),
        Input(command="git grep needle -- :/internal"): Allow(),
        Input(command="git grep --help"): Allow(),
        Input(command="git --help grep"): Allow(),
        Input(command="git grep"): Allow(),
        Input(command="echo 'git grep needle'"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        *(
            CommandMatches(pattern.schema, only_if=pattern.only_if, skip_if=pattern.skip_if)
            for pattern in FILES_RULE.patterns
        ),
    ],
    message=(FILES_RULE.message),
    block=True,
    max_fires=None,
    tests={
        Input(
            command="rg --files -g AGENTS.md -g CLAUDE.md "
            "/Users/yasyf/.claude/worktrees/captain-hook/language-shared-cache "
            "/Users/yasyf/.claude/worktrees/captain-hook /Users/yasyf/.claude/worktrees"
        ): Block(pattern="blocked even with"),
        Input(command="rg --files /Users/yasyf/.claude/worktrees/captain-hook"): Block(),
        Input(command="rg --files -g AGENTS.md /Users/yasyf/.claude/worktrees/captain-hook"): Block(),
        Input(command="rg --files /Users/yasyf/.claude/worktrees"): Block(),
        Input(command="rg --files --glob=CLAUDE.md /Users/yasyf/.claude/worktrees"): Block(),
        Input(command="rg --files -gAGENTS.md ~/.orca/workspaces/captain-hook"): Block(),
        Input(command="rg --files ~"): Block(),
        Input(command="rg --files -g '*.py' ~"): Block(),
        Input(command="rg --files --iglob=agents.md -tmd ~"): Block(),
        Input(command='rg --files "$HOME"'): Block(),
        Input(command="cd ~ && rg --files"): Block(),
        Input(command="rg --files src ~/.claude/worktrees", cwd="/repo"): Block(),
        Input(command="sh -c 'rg --files -g AGENTS.md ~/.claude/worktrees' | head -5"): Block(),
        Input(command="rg --type-list --files ~"): Block(),
        Input(command="rg --generate=man --files ~"): Block(),
        Input(command="rg --generate man --files ~"): Block(),
        Input(command="rg --files --type-list --files ~"): Block(),
        Input(command="rg --files --type-list -nC3 --files ~"): Block(),
        Input(command="rg --files ~ --type-list -nC3 --files"): Block(),
        Input(command='rg --files -g AGENTS.md "$POOL"'): Block(),
        Input(command="rg --files /"): Block(),
        Input(command="rg --files ~/.."): Block(),
        Input(command="rg --files ~/.claude"): Block(),
        Input(command="rg --files -g AGENTS.md ~/.claude"): Block(),
        Input(command="rg --files ~/.orca"): Block(),
        Input(command="cd ~/.claude && rg --files"): Block(),
        Input(command="rg --files .claude/worktrees", cwd="/repo"): Block(),
        Input(command="rg --files -g AGENTS.md .claude/worktrees/stale", cwd="/repo"): Block(),
        Input(command="rg --files ~/repos/claude/worktrees"): Block(),
        Input(command="rg --files -g AGENTS.md ~/repos/claude/worktrees/captain-hook"): Block(),
        Input(command="rg --files ~/repos/claude/worktrees/captain-hook/lane"): Allow(),
        Input(command="rg --files ~/repos/orca/workspaces"): Block(),
        Input(command="rg --files ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17"): Block(),
        Input(
            command="rg --files -g AGENTS.md ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17/captain-hook"
        ): Block(),
        Input(command="rg --files ~/repos/orca"): Block(),
        Input(command="rg --files ~/repos"): Block(),
        Input(
            command="rg --files -g AGENTS.md /Users/yasyf/.claude/worktrees/captain-hook/language-shared-cache"
        ): Allow(),
        Input(command="rg --files ~/.claude/worktrees/captain-hook/lane"): Allow(),
        Input(command="rg --files ~/.claude/worktrees/captain-hook/lane/src"): Allow(),
        Input(command="rg --files ~/.claude/scratch"): Allow(),
        Input(command="rg --files ~/.config"): Allow(),
        Input(
            command="rg --files ~/repos/orca/workspaces/7f3c2a10-5d4e-4b8a-9c61-2e0f8a9b4d17/captain-hook/lane"
        ): Allow(),
        Input(command="rg --files .", cwd="/repo"): Allow(),
        Input(command="rg --files", cwd="/repo"): Allow(),
        Input(command="rg --files -g AGENTS.md src/internal", cwd="/repo"): Allow(),
        Input(command="rg --files /tmp/scratch-lane"): Allow(),
        Input(command="rg --files --help ~"): Allow(),
        Input(command="rg --type-list"): Allow(),
        Input(command="rg --files --type-list ~"): Allow(),
        Input(command="rg --files --generate=man ~"): Allow(),
        Input(command="rg --files --generate man ~"): Allow(),
        Input(command="rg needle ~/.claude/worktrees"): Allow(),
        Input(command="echo 'rg --files ~'"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        *(
            CommandMatches(pattern.schema, only_if=pattern.only_if, skip_if=pattern.skip_if)
            for pattern in FIND_RULE.patterns
        ),
    ],
    message=(FIND_RULE.message),
    block=True,
    max_fires=None,
    tests={
        Input(
            command="cd ~/.claude/worktrees/captain-hook/dispatch-stall && "
            'grep -n "daemonkit" go.mod go.sum 2>/dev/null | head -5; '
            'find / -type d -iname "daemonkit*" 2>/dev/null | grep -v Trash | head -10'
        ): Block(pattern="broad filesystem search"),
        Input(command='find / -type d -iname "daemonkit*" -not -path "*/Trash/*" 2>/dev/null | head -10'): Block(),
        Input(command="find / -name daemonkit"): Block(),
        Input(command="find ~ -type d -name daemonkit"): Block(),
        Input(command='find "$HOME" -type d -name daemonkit'): Block(),
        Input(command='find "${HOME}" -type d -name daemonkit'): Block(),
        Input(command="find /Users -name daemonkit"): Block(),
        Input(command="find /Users/alice -name daemonkit"): Block(),
        Input(command="find /home/alice -name daemonkit"): Block(),
        Input(command="find ~/.claude/worktrees -name daemonkit"): Block(),
        Input(command="find $HOME/.claude/worktrees -name daemonkit"): Block(),
        Input(command="find /System/Volumes/Data -name daemonkit"): Block(),
        Input(command="find -H / -name daemonkit"): Block(),
        Input(command="find -HL / -name daemonkit", cwd="/repo"): Block(),
        Input(command="find $(printf /) -name daemonkit", cwd="/repo"): Block(),
        Input(command="find / -maxdepth 1 $EXPR"): Block(),
        Input(command="find / -maxdepth 1 -name $EXPR"): Block(),
        Input(command=r"find / -exec echo + -maxdepth 1 \;"): Block(),
        Input(command="find -f / -name daemonkit"): Block(),
        Input(command="find src / -name daemonkit"): Block(),
        Input(command="sudo /usr/bin/find / -name daemonkit"): Block(),
        Input(command="sh -c 'find / -name daemonkit'"): Block(),
        Input(command="cd / && find . -name daemonkit"): Block(),
        Input(command="find . -name daemonkit", cwd="/"): Block(),
        Input(command="find -name daemonkit", cwd="/"): Block(),
        Input(command="find / -maxdepth 99 -name daemonkit"): Block(),
        Input(
            command="ls ~/.claude/plugins/cache | head; find / -maxdepth 8 -type d -name captain_hook | head -5"
        ): Block(),
        Input(command="find ~ -maxdepth 4 -path '*cc-slack*' -name 'state.db*' 2>/dev/null | head"): Block(),
        Input(command="d=$(ls -d */*/ | sort -V | tail -1); find $d -name '*poll*'", cwd="/p"): Block(),
        Input(command="S=$(mktemp -d); cd $S; for d in a1 b2; do find ecr-$d -maxdepth 4 | head -8; done"): Block(),
        Input(command='outd=$(mktemp -d -t move-exec.XXXXXX) && find "$outd" -type f | head -5'): Block(),
        Input(command="latest=$(ls -d getaway/getaway/*/ | tail -1); find $latest -name hooks.json", cwd="/p"): Block(),
        Input(command="P=$(ls -d ~/.daemonkit/tools/capt-hook/*/ | tail -1); find $P -name '*.py'"): Block(),
        Input(command="W=~/.claude/worktrees; find $W -name daemonkit"): Block(),
        Input(command="for w in ~ /; do find $w -name daemonkit; done"): Block(),
        Input(command="cd / && for d in *; do find $d -name daemonkit; done"): Block(),
        Input(command="find ~/.claude/worktrees/*/ -name daemonkit"): Block(),
        Input(command="find /Users/* -maxdepth 3 -name daemonkit"): Block(),
        Input(command='D=$(printf /); cd "$D" && find . -name daemonkit'): Block(),
        Input(command="find . -name daemonkit"): Block(),
        Input(
            command=(
                "for d in go/ci/internal/release/*/; do "
                "echo \"$(find $d -name '*.go' ! -name '*_test.go' | xargs cat | wc -l) $d\"; done"
            ),
            cwd="/Users/yasyf/.claude/worktrees/monorepo/ethos-audit",
        ): Block(),
        Input(
            command=(
                "cd /tmp && for d in a b c d e f g h i j k l m n o p q r s /; do find $d -name '*.go'; done "
                "2>/dev/null | head"
            )
        ): Block(),
        Input(
            command=(
                "cd /Users/yasyf/.claude/worktrees/monorepo/parity-audit-dev/go/ci/internal/release && for d in "
                "selection dag judge trailers targets stacks images pipeline prcheck reach reads record platy render "
                "slack hold bake ledger canvas finish; do echo \"== $d\"; find $d -name '*.go' ! -name '*_test.go' | "
                "xargs wc -l | sort -n | tail -25; done 2>/dev/null | grep -v ' total$'"
            ),
            cwd="/Users/yasyf/.orca/workspaces/monorepo/monorepo/sole",
        ): Allow(),
        Input(command="find / -maxdepth 1 -type d"): Allow(),
        Input(command="find ~ -maxdepth 2 -type d -name daemonkit"): Allow(),
        Input(command="find / -maxdepth 0"): Allow(),
        Input(command="find . -type d -name daemonkit", cwd="/repo"): Allow(),
        Input(command="find ~/Code/captain-hook -type d -name daemonkit"): Allow(),
        Input(command="find ~/.claude/worktrees/captain-hook/dispatch-stall -name daemonkit"): Allow(),
        Input(
            command=(
                "R=/Users/yasyf/.claude/worktrees/monorepo/parity-audit-dev; ls $R/go/ci; "
                "find $R -name 'targets.yaml' -not -path '*/node_modules/*' | head"
            )
        ): Allow(),
        Input(
            command="for v in 0.2.9 0.2.11; do find $v -maxdepth 3; done",
            cwd="/Users/yasyf/.claude/plugins/cache/forge",
        ): Allow(),
        Input(
            command=(
                "I=~/.local/share/uv/tools/capt-hook/lib/python3.13/site-packages/captain_hook; "
                "find $I/builtin_packs -name '*.py' -o -name '*.md' | sort | xargs wc -l"
            )
        ): Allow(),
        Input(
            command="for d in active-alert lane-comms; do find ~/.claude/worktrees/cc-skills/$d -name 'brief.md'; done"
        ): Allow(),
        Input(
            command="for d in selection dag judge; do find $d -name '*.go' ! -name '*_test.go' | xargs wc -l; done",
            cwd="/Users/yasyf/.claude/worktrees/monorepo/parity-audit-dev/go/ci/internal/release",
        ): Allow(),
        Input(command="find $HOME/.claude/worktrees/captain-hook/x -name daemonkit"): Allow(),
        Input(command="find ~/.claude/worktrees/captain-hook/* -name daemonkit"): Block(),
        Input(command="find src -name daemonkit", cwd="/repo"): Allow(),
        Input(command="find src -newermt 2026-09-17", cwd="/repo"): Allow(),
        Input(command="find src -path / -prune", cwd="/repo"): Allow(),
        Input(command="echo 'find / -name daemonkit'"): Allow(),
        Input(command="cat <<'EOF'\nfind / -name daemonkit\nEOF"): Allow(),
        Input(command="ccx repo locate daemonkit"): Allow(),
    },
)
