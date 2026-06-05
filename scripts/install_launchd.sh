#!/bin/bash
# Render launchd plist templates with the current machine's absolute paths and install them.
#
# The committed launchd/*.plist files are TEMPLATES containing placeholders:
#   __PROJECT_DIR__   -> this repo's absolute path (auto-derived)
#   __NODE_BIN_DIR__  -> directory holding the `claude` CLI binary (where node/nvm put it)
# launchd does not expand $HOME or env vars inside plist values, so we substitute them here
# at install time. The real paths therefore live only in the environment, never in git.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
mkdir -p "$DEST" "$DIR/logs"

# Load .env (for an optional NODE_BIN_DIR override) without leaking vars globally.
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi

# Resolve the directory that holds the `claude` CLI.
# Priority: NODE_BIN_DIR (env / .env)  >  `which claude`.
if [ -n "${NODE_BIN_DIR:-}" ]; then
  node_bin_dir="$NODE_BIN_DIR"
else
  claude_path="$(command -v claude 2>/dev/null || true)"
  if [ -z "$claude_path" ]; then
    echo "ERROR: 'claude' CLI not found on PATH and NODE_BIN_DIR not set in .env." >&2
    echo "  Install Claude Code and run 'claude login', or set NODE_BIN_DIR in .env." >&2
    exit 1
  fi
  node_bin_dir="$(dirname "$claude_path")"
fi
echo "PROJECT_DIR  = $DIR"
echo "NODE_BIN_DIR = $node_bin_dir"
echo

# Jobs to install. Add/remove labels here; matching templates must exist in launchd/.
# (macro*, monitor_* templates also exist but are left out of the default schedule.)
for label in us us_top20 kr kr_top20 kr_deepdive insight radar pulse chart_lesson bot; do
  src="$DIR/launchd/com.user.stockreporter.$label.plist"
  dst="$DEST/com.user.stockreporter.$label.plist"
  if [ ! -f "$src" ]; then
    echo "SKIP (no template): $src" >&2
    continue
  fi
  sed -e "s#__PROJECT_DIR__#${DIR}#g" \
      -e "s#__NODE_BIN_DIR__#${node_bin_dir}#g" \
      "$src" > "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "Installed: $dst"
done

echo
echo "Active jobs:"
launchctl list | grep stockreporter || true
