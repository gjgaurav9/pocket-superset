#!/bin/bash
# Stop the service and remove it from login items. Paired devices and the audit log in
# ~/.pocket-superset stay unless you delete that folder yourself.
set -uo pipefail
LABEL="local.pocket-superset"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/.pocket-superset/app"
echo "Pocket Superset service removed."
