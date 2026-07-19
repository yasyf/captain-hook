#!/usr/bin/env bash
# Exercise the installed signed bridge. Usage: notify-test.sh [ping|notify]
set -euo pipefail

sock="${CAPT_HOOK_HELPER_DIR:-$HOME/.capt-hook}/helper.sock"
bridge="/Applications/Captain Hook.app/Contents/Helpers/capt-hook-helper-client"

case "${1:-ping}" in
  ping)
    "$bridge" --socket "$sock" ping
    ;;
  notify)
    "$bridge" --socket "$sock" notify <<'JSON'
{"kind":"pr_open","title":"Block force-pushes","subtitle":"captain-hook","body":"Rule guard-rm-rf opened","url":"https://github.com/yasyf/captain-hook/pull/12","repo":"github.com/yasyf/captain-hook"}
JSON
    ;;
  *)
    echo "usage: notify-test.sh [ping|notify]" >&2
    exit 2
    ;;
esac
