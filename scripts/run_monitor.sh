#!/bin/bash
# Usage: run_monitor.sh [intraday|daily]
# 실패 시 모니터 채널로 알림 (notify_failure). 로그 파일은 launchd_monitor_<mode>.err 규칙.
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

MODE="${1:-unknown}"
JOB="monitor_${MODE}"

export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090
    . "$NVM_DIR/nvm.sh"
fi

./venv/bin/python -m src.monitor "$@"
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
    JOB="$JOB" EXIT="$EXIT" ERR_LOG="$DIR/logs/launchd_${JOB}.err" \
        ./venv/bin/python -m src.notify_failure || true
fi

exit "$EXIT"
