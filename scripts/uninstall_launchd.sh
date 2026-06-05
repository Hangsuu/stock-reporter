#!/bin/bash
set -euo pipefail
for label in us us_top20 kr kr_top20 kr_deepdive insight radar pulse chart_lesson bot; do
  plist="$HOME/Library/LaunchAgents/com.user.stockreporter.$label.plist"
  if [ -f "$plist" ]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm "$plist"
    echo "Removed: $plist"
  fi
done
