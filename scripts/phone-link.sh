#!/bin/bash
# Give the bridge a private HTTPS address on your tailnet, reachable only from your own
# devices. Uses Tailscale Serve, never Funnel (Funnel would put it on the public internet).
set -euo pipefail
PORT="${POCKET_PORT:-8780}"

if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale is not running on this Mac. Open the Tailscale app and sign in, then run this again."
  exit 1
fi
tailscale serve --bg "$PORT"
echo
tailscale serve status
echo
echo "On the phone: install Tailscale, sign in with the same account, then open the https address above in Chrome."
echo "Pair it: open http://127.0.0.1:$PORT/local/pair on this Mac and type the code on the phone."
echo "Install: Chrome menu, then Add to Home screen."
