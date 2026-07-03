#!/usr/bin/env bash
# Regenerates docs/assets/demo.png — a freeze render of a real captured run.
#
# Stages a scratch scaffold in a temp dir (never this repo's own hooks), writes
# one force-push guard with inline tests, replays the exact PreToolUse payload
# Claude Code sends when the agent runs `git push --force`, then runs the
# inline tests. Requires: uv, jq, freeze (https://github.com/charmbracelet/freeze).
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

mkdir -p "$scratch/.claude/hooks"
cat > "$scratch/.claude/hooks/safety.py" <<'HOOK'
from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "push", "--force"],
    reason="Force-pushing rewrites shared history",
    hint="Use `git push --force-with-lease` instead",
    tests={
        Input(command="git push --force"): Block(),
        Input(command="git push origin main"): Allow(),
    },
)
HOOK

cat > "$scratch/transcript.sh" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
bold=$'\e[1m'
reset=$'\e[0m'

printf "%s\$ echo '{\"tool_name\": \"Bash\", \"tool_input\": {\"command\": \"git push --force\"}}' |%s\n" "$bold" "$reset"
printf '%s    uvx capt-hook run PreToolUse | jq -r .hookSpecificOutput.permissionDecisionReason%s\n' "$bold" "$reset"
echo '{"tool_name": "Bash", "tool_input": {"command": "git push --force"}}' \
  | uvx capt-hook run PreToolUse 2>/dev/null | jq -r .hookSpecificOutput.permissionDecisionReason
printf '\n'
printf '%s$ uvx capt-hook test%s\n' "$bold" "$reset"
uvx capt-hook test 2>/dev/null
SCRIPT

cd "$scratch"
freeze --execute "bash $scratch/transcript.sh" \
  --theme github-dark --background "#0d1117" --window --padding 24 --font.size 28 \
  --width 1480 -o "$repo/docs/assets/demo.png"
