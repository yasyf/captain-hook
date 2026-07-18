#!/usr/bin/env bash
# Exercise helper.sock v1 with nc(1). Usage: notify-test.sh [ping|notify]
set -euo pipefail

sock="${CAPT_HOOK_HELPER_DIR:-$HOME/.capt-hook}/helper.sock"

case "${1:-ping}" in
  ping)
    printf '%s\n' '{"v":1,"op":"ping"}' | nc -U "$sock"
    ;;
  notify)
    printf '%s\n' '{"v":1,"op":"notify","kind":"pr_open","title":"Block force-pushes","subtitle":"captain-hook","body":"Rule guard-rm-rf opened","url":"https://github.com/yasyf/captain-hook/pull/12","repo":"github.com/yasyf/captain-hook"}' | nc -U "$sock"
    ;;
  *)
    echo "usage: notify-test.sh [ping|notify]" >&2
    exit 2
    ;;
esac
