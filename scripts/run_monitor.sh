#!/bin/bash
# Usage: run_monitor.sh [intraday|daily]
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090
    . "$NVM_DIR/nvm.sh"
fi

exec ./venv/bin/python -m src.monitor "$@"
