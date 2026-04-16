#!/usr/bin/env bash
# rud_server_setup.sh — Install and configure ngrok tunnels on the RUD server
# Run this ON the server (Ubuntu/Debian): bash rud_server_setup.sh
set -euo pipefail

NGROK_AUTH_TOKEN="${NGROK_AUTH_TOKEN:?'Set NGROK_AUTH_TOKEN env var before running'}"
NGROK_CONFIG="/etc/ngrok.yml"
NGROK_SERVICE="ngrok-aura"

echo "=== RUD Server Setup — AURA ngrok tunnels ==="

# ── 1. Install ngrok if not present ──────────────────────────────────────────
if ! command -v ngrok &>/dev/null; then
  echo "[1/5] Installing ngrok..."
  curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
    | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
  echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
    | sudo tee /etc/apt/sources.list.d/ngrok.list
  sudo apt update -qq
  sudo apt install ngrok -y
  echo "[1/5] ngrok installed: $(ngrok version)"
else
  echo "[1/5] ngrok already installed: $(ngrok version)"
fi

# ── 2. Configure ngrok auth token ────────────────────────────────────────────
echo "[2/5] Configuring ngrok auth token..."
ngrok config add-authtoken "${NGROK_AUTH_TOKEN}"

# ── 3. Create /etc/ngrok.yml with tunnel definitions ─────────────────────────
echo "[3/5] Writing ${NGROK_CONFIG}..."
sudo tee "${NGROK_CONFIG}" > /dev/null <<'NGROK_YAML'
version: "3"
agent:
  authtoken: ${NGROK_AUTH_TOKEN}

tunnels:
  ssh:
    proto: tcp
    addr: 22
  ollama:
    proto: http
    addr: 11434
  n8n:
    proto: http
    addr: 5678
  grafana:
    proto: http
    addr: 3200
  portainer:
    proto: http
    addr: 9443
NGROK_YAML

echo "[3/5] ${NGROK_CONFIG} written."

# ── 4. Create systemd service ─────────────────────────────────────────────────
echo "[4/5] Creating systemd service /etc/systemd/system/${NGROK_SERVICE}.service..."
sudo tee "/etc/systemd/system/${NGROK_SERVICE}.service" > /dev/null <<SERVICE
[Unit]
Description=AURA ngrok tunnels (SSH + Ollama + N8N + Grafana + Portainer)
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ngrok start --all --config ${NGROK_CONFIG}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
# Run as root so ngrok can bind privileged ports if needed
User=root

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable "${NGROK_SERVICE}"
echo "[4/5] Service created and enabled."

# ── 5. Start the service ──────────────────────────────────────────────────────
echo "[5/5] Starting ${NGROK_SERVICE}..."
sudo systemctl restart "${NGROK_SERVICE}"

# Give ngrok a few seconds to establish tunnels
sleep 5

echo ""
echo "=== Tunnel URLs ==="
if ngrok_pid=$(pgrep -f "ngrok start" 2>/dev/null | head -1); then
  # Query the ngrok local API for tunnel info
  if curl -s http://localhost:4040/api/tunnels &>/dev/null; then
    curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys, json
data = json.load(sys.stdin)
tunnels = data.get('tunnels', [])
if not tunnels:
    print('No tunnels found yet — check: curl http://localhost:4040/api/tunnels')
else:
    for t in tunnels:
        name = t.get('name', '?')
        url  = t.get('public_url', '?')
        print(f'  {name:<12} → {url}')
" 2>/dev/null || echo "  (install python3 for pretty output)"
  else
    echo "  ngrok API not ready yet. Check with:"
    echo "  curl http://localhost:4040/api/tunnels"
  fi
else
  echo "  ngrok not running. Check logs with:"
  echo "  sudo journalctl -u ${NGROK_SERVICE} -f"
fi

echo ""
echo "=== Service Status ==="
sudo systemctl status "${NGROK_SERVICE}" --no-pager -l || true

echo ""
echo "Done! To check tunnels later: curl http://localhost:4040/api/tunnels"
echo "To view logs: sudo journalctl -u ${NGROK_SERVICE} -f"
