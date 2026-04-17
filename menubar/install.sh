#!/bin/bash
# Install AURA menu bar app
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$REPO_DIR/.venv/bin/python3"

# Verify Python exists
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Project venv not found at $REPO_DIR/.venv"
    echo "Run 'uv sync' in the repo root first."
    exit 1
fi

echo "Installing rumps and httpx via uv..."
cd "$REPO_DIR"
uv add rumps httpx 2>&1 | tail -3

echo "Copying LaunchAgent plist..."
cp "$SCRIPT_DIR/com.aura.menubar.plist" ~/Library/LaunchAgents/

# Unload first if already running (ignore errors)
launchctl unload ~/Library/LaunchAgents/com.aura.menubar.plist 2>/dev/null || true

echo "Loading LaunchAgent..."
launchctl load ~/Library/LaunchAgents/com.aura.menubar.plist

echo ""
echo "✅ AURA menu bar instalado."
echo "   Inicia ahora: launchctl start com.aura.menubar"
echo "   Detener:      launchctl stop com.aura.menubar"
echo "   Logs:         tail -f /tmp/aura-menubar.log /tmp/aura-menubar-err.log"
