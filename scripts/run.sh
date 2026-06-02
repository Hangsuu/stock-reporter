#!/bin/bash
# Usage: run.sh us | kr [--dry-run] [--force]
# launchd 잡 실패 시 모니터 채널로 텔레그램 알림을 보낸다 (notify_failure).
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

JOB="${1:-unknown}"

# Make `claude` (Claude Code CLI, installed via nvm) discoverable from launchd
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090
    . "$NVM_DIR/nvm.sh"
fi

./venv/bin/python -m src.reporter "$@"
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
    # 실패 알림: notifier만 의존하므로 claude/analyst가 깨져도 동작.
    JOB="$JOB" EXIT="$EXIT" ERR_LOG="$DIR/logs/launchd_${JOB}.err" \
        ./venv/bin/python -m src.notify_failure || true
fi

exit "$EXIT"
