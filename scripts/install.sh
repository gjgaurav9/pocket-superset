#!/bin/bash
# Install Pocket Superset as a background service (launchd) that starts at login.
# The app is copied to ~/.pocket-superset/app because launchd cannot start programs from
# Desktop, Documents or Downloads.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$HOME/.pocket-superset"
DEST="$DATA/app"
LABEL="local.pocket-superset"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${POCKET_PORT:-8780}"
PY="$(command -v python3)"
SUPERSET_DIR="$(dirname "$(command -v superset || echo "$HOME/.paradev/bin/superset")")"

mkdir -p "$DEST/app" "$HOME/Library/LaunchAgents"
cp "$SRC/server.py" "$DEST/server.py"
cp "$SRC"/app/* "$DEST/app/"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DEST/server.py</string><string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$SUPERSET_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DATA/server.log</string>
  <key>StandardErrorPath</key><string>$DATA/server.log</string>
</dict></plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
echo "Pocket Superset is running on http://127.0.0.1:$PORT"
echo "Log: $DATA/server.log"
tail -n 3 "$DATA/server.log" || true
echo
echo "If the log says 'Not logged in', store a Paradev API key for the service:"
echo "  security add-generic-password -U -s pocket-superset-paradev -a \"\$USER\" -w 'sk_live_…'"
echo "  then run this script again."
