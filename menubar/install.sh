#!/bin/bash
# Install AURA menu bar app
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/Users/oxyzen/.local/share/uv/tools/claude-code-telegram/bin/python3"

# Verify Python exists
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python not found at $PYTHON"
    echo "Run 'uv tool install claude-code-telegram' first, or update PYTHON path in this script."
    exit 1
fi

echo "Installing rumps and httpx..."
"$PYTHON" -m pip install rumps httpx --quiet

echo "Copying LaunchAgent plist..."
cp "$SCRIPT_DIR/com.aura.menubar.plist" ~/Library/LaunchAgents/

echo "Loading LaunchAgent..."
launchctl load ~/Library/LaunchAgents/com.aura.menubar.plist

echo "AURA menu bar installed. It will start automatically on login."
echo "   To start now: launchctl start com.aura.menubar"
echo "   To stop:      launchctl stop com.aura.menubar"
echo "   Logs:         tail -f /tmp/aura-menubar.log /tmp/aura-menubar-err.log"
