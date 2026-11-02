#!/usr/bin/env bash
# start_webhook.sh — Start the DeerFlow webhook receiver + public tunnel
#
# Usage:
#   chmod +x start_webhook.sh
#   ./start_webhook.sh
#
# Prerequisites (install once):
#   pip install fastapi uvicorn anthropic httpx slack-sdk google-api-python-client google-auth
#   brew install cloudflared        # macOS
#   # or: brew install ngrok/ngrok/ngrok && ngrok config add-authtoken YOUR_TOKEN
#
# The script will print the public HTTPS URL — paste this into:
#   Gmail: GCP Pub/Sub push subscription endpoint
#   Slack:  api.slack.com/apps → Event Subscriptions → Request URL

set -euo pipefail

PORT="${WEBHOOK_PORT:-8080}"
TUNNEL="${TUNNEL:-cloudflared}"   # or: ngrok

# ── Sanity checks ────────────────────────────────────────────────────────────
if ! command -v uvicorn &>/dev/null; then
  echo "ERROR: uvicorn not found. Run: pip install fastapi uvicorn anthropic httpx slack-sdk google-api-python-client google-auth"
  exit 1
fi

if ! command -v "$TUNNEL" &>/dev/null; then
  echo "ERROR: $TUNNEL not found."
  if [ "$TUNNEL" = "cloudflared" ]; then
    echo "  macOS: brew install cloudflared"
    echo "  Linux: curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && dpkg -i cloudflared.deb"
  else
    echo "  macOS: brew install ngrok/ngrok/ngrok"
  fi
  exit 1
fi

# ── Start uvicorn in background ───────────────────────────────────────────────
echo "Starting webhook receiver on port $PORT..."
uvicorn webhook_receiver:app --host 0.0.0.0 --port "$PORT" --log-level info &
UVICORN_PID=$!
echo "Uvicorn PID: $UVICORN_PID"

# Give uvicorn a moment to start
sleep 2

# ── Start tunnel ──────────────────────────────────────────────────────────────
echo ""
echo "Starting $TUNNEL tunnel..."
echo "──────────────────────────────────────────────────"

if [ "$TUNNEL" = "cloudflared" ]; then
  # cloudflared prints the public URL to stderr
  cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | tee /tmp/cloudflared_output &
  TUNNEL_PID=$!

  # Wait for URL to appear
  echo "Waiting for tunnel URL..."
  for i in $(seq 1 30); do
    URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cloudflared_output 2>/dev/null | head -1 || true)
    if [ -n "$URL" ]; then
      break
    fi
    sleep 1
  done

  if [ -z "$URL" ]; then
    echo "WARNING: Could not detect public URL automatically. Check cloudflared output above."
    URL="<check cloudflared output above>"
  fi

else
  # ngrok
  ngrok http "$PORT" --log=stdout --log-format=json > /tmp/ngrok_output.json &
  TUNNEL_PID=$!

  echo "Waiting for ngrok tunnel..."
  for i in $(seq 1 15); do
    URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null || true)
    if [ -n "$URL" ]; then
      break
    fi
    sleep 1
  done

  if [ -z "$URL" ]; then
    echo "WARNING: Could not detect ngrok URL. Check http://localhost:4040"
    URL="<check http://localhost:4040>"
  fi
fi

echo ""
echo "════════════════════════════════════════════════════"
echo "  WEBHOOK RECEIVER IS LIVE"
echo ""
echo "  Public URL: $URL"
echo ""
echo "  Gmail endpoint: $URL/webhook/gmail"
echo "  Slack endpoint: $URL/webhook/slack"
echo "  Health check:   $URL/health"
echo ""
echo "  Next steps:"
echo "  1. Gmail: Set this as your Pub/Sub push subscription endpoint:"
echo "       $URL/webhook/gmail"
echo "  2. Slack: Paste into api.slack.com/apps → Event Subscriptions:"
echo "       $URL/webhook/slack"
echo "════════════════════════════════════════════════════"
echo ""
echo "Press Ctrl+C to stop."

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "Shutting down..."
  kill $UVICORN_PID 2>/dev/null || true
  kill $TUNNEL_PID 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

# Keep running
wait $UVICORN_PID
