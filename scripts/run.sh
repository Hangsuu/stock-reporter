#!/bin/bash
# Usage: run.sh us | kr [--dry-run] [--force]
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# Make `claude` (Claude Code CLI, installed via nvm) discoverable from launchd
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090
    . "$NVM_DIR/nvm.sh"
fi

exec ./venv/bin/python -m src.reporter "$@"
