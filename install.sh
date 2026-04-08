#!/usr/bin/env bash
#
# AURA Installer — Personal AI Agent
# Installs AURA and lets you pick which brains to enable.
#
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Symbols
CHECK="${GREEN}✓${NC}"
CROSS="${RED}✗${NC}"
ARROW="${CYAN}→${NC}"

echo ""
echo -e "${BOLD}${PURPLE}  █████╗ ██╗   ██╗██████╗  █████╗ ${NC}"
echo -e "${BOLD}${PURPLE} ██╔══██╗██║   ██║██╔══██╗██╔══██╗${NC}"
echo -e "${BOLD}${PURPLE} ███████║██║   ██║██████╔╝███████║${NC}"
echo -e "${BOLD}${PURPLE} ██╔══██║██║   ██║██╔══██╗██╔══██║${NC}"
echo -e "${BOLD}${PURPLE} ██║  ██║╚██████╔╝██║  ██║██║  ██║${NC}"
echo -e "${BOLD}${PURPLE} ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝${NC}"
echo ""
echo -e "${BOLD}  Multi-Brain Personal AI Agent${NC}"
echo -e "  ${CYAN}Zero API costs · CLI subscription auth${NC}"
echo ""

# ─── Prerequisites check ───
echo -e "${BOLD}Checking prerequisites...${NC}"

check_cmd() {
    if command -v "$1" &>/dev/null; then
        echo -e "  ${CHECK} $1 found"
        return 0
    else
        echo -e "  ${CROSS} $1 not found"
        return 1
    fi
}

MISSING=()
check_cmd "node" || MISSING+=("node")
check_cmd "npm" || MISSING+=("npm")
check_cmd "python3" || MISSING+=("python3")
check_cmd "git" || MISSING+=("git")

# Check for uv
if ! check_cmd "uv"; then
    echo -e "  ${ARROW} Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    echo ""
    echo -e "${RED}Missing: ${MISSING[*]}${NC}"
    echo "Install them first, then re-run this script."
    exit 1
fi

echo ""

# ─── Telegram Bot Token ───
echo -e "${BOLD}Telegram Bot Setup${NC}"
echo -e "  Create a bot via ${CYAN}@BotFather${NC} on Telegram first."
echo ""
read -p "  Telegram Bot Token: " BOT_TOKEN
read -p "  Your Telegram User ID: " USER_ID

if [ -z "$BOT_TOKEN" ] || [ -z "$USER_ID" ]; then
    echo -e "${RED}Bot token and user ID are required.${NC}"
    exit 1
fi

echo ""

# ─── Brain Selection ───
echo -e "${BOLD}Select your brains:${NC}"
echo ""
echo -e "  ${YELLOW}🟠 Claude${NC}    — Anthropic (Pro \$20/mo or Max \$100/mo)"
echo -e "                ${CYAN}Primary brain. Full SDK, tools, streaming.${NC}"
echo -e "                Auth: ${GREEN}claude auth login${NC} (subscription-based, no API key)"
echo ""
echo -e "  ${YELLOW}🟢 Codex${NC}     — OpenAI (Plus \$20/mo)"
echo -e "                ${CYAN}Coding specialist. Fast code generation.${NC}"
echo -e "                Auth: ${GREEN}codex login${NC} (subscription-based, no API key)"
echo ""
echo -e "  ${YELLOW}🔵 Gemini${NC}    — Google (Free, 1000 req/day)"
echo -e "                ${CYAN}Multimodal. Long context. Free tier.${NC}"
echo -e "                Auth: ${GREEN}gemini${NC} → browser login (Google account)"
echo ""
echo -e "  ${YELLOW}🟣 Perplexity${NC} — Search AI (Pro \$20/mo, includes \$5 API credits)"
echo -e "                ${CYAN}Real-time web search. Research assistant.${NC}"
echo -e "                Auth: ${GREEN}API key${NC} from perplexity.ai/settings"
echo ""

BRAINS=("claude")  # Claude is always installed (primary)

read -p "  Install Codex (OpenAI)? [y/N]: " INSTALL_CODEX
read -p "  Install Gemini (Google)? [y/N]: " INSTALL_GEMINI
read -p "  Install Perplexity (Search)? [y/N]: " INSTALL_PERPLEXITY

echo ""

# ─── Install AURA core ───
echo -e "${BOLD}Installing AURA core...${NC}"

INSTALL_DIR="$HOME/claude-code-telegram"

if [ ! -d "$INSTALL_DIR" ]; then
    echo -e "  ${ARROW} Cloning repository..."
    git clone https://github.com/royaluniondesign-sys/aura.git "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

echo -e "  ${ARROW} Installing with uv (editable mode)..."
uv tool install --force --editable . 2>/dev/null || uv pip install -e .

# ─── Configure .env ───
echo -e "  ${ARROW} Configuring .env..."
cat > "$INSTALL_DIR/.env" << ENVEOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
ALLOWED_USERS=$USER_ID
APPROVED_DIRECTORY=$HOME
CLAUDE_MAX_TURNS=50
AGENTIC_MODE=true
ENABLE_MCP=false
ENABLE_SCHEDULER=true
NOTIFICATION_CHAT_IDS=$USER_ID
ENVEOF

echo -e "  ${CHECK} Core installed"

# ─── Install brains ───
echo ""
echo -e "${BOLD}Installing brains...${NC}"

# Claude (always)
echo -e "  ${CHECK} 🟠 Claude — checking..."
if command -v claude &>/dev/null; then
    echo -e "  ${CHECK} Claude CLI found ($(claude --version 2>/dev/null | head -1))"
else
    echo -e "  ${ARROW} Install Claude: ${GREEN}npm install -g @anthropic-ai/claude-code${NC}"
    echo -e "  ${ARROW} Then run: ${GREEN}claude auth login${NC}"
fi

# Codex
if [[ "${INSTALL_CODEX:-n}" =~ ^[Yy] ]]; then
    BRAINS+=("codex")
    echo -e "  ${ARROW} 🟢 Installing Codex CLI..."
    npm install -g @openai/codex 2>/dev/null && echo -e "  ${CHECK} Codex installed" || echo -e "  ${CROSS} Codex install failed"
    echo -e "  ${ARROW} Run ${GREEN}codex login${NC} to authenticate"
fi

# Gemini
if [[ "${INSTALL_GEMINI:-n}" =~ ^[Yy] ]]; then
    BRAINS+=("gemini")
    echo -e "  ${ARROW} 🔵 Installing Gemini CLI..."
    npm install -g @anthropic-ai/gemini-cli 2>/dev/null || npm install -g @anthropic-ai/claude-code 2>/dev/null
    # Try the correct package
    npm install -g @google/gemini-cli 2>/dev/null && echo -e "  ${CHECK} Gemini installed" || echo -e "  ${ARROW} Install manually: npm install -g @google/gemini-cli"
    echo -e "  ${ARROW} Run ${GREEN}gemini${NC} to authenticate via browser"
fi

# Perplexity
if [[ "${INSTALL_PERPLEXITY:-n}" =~ ^[Yy] ]]; then
    BRAINS+=("perplexity")
    echo -e "  ${ARROW} 🟣 Installing Perplexity CLI..."
    uv tool install pplx-cli 2>/dev/null && echo -e "  ${CHECK} Perplexity installed" || echo -e "  ${CROSS} Perplexity install failed"
    echo -e "  ${ARROW} Run ${GREEN}perplexity setup${NC} and enter your API key"
fi

echo ""

# ─── Google Workspace (optional) ───
read -p "  Setup Gmail/Calendar integration? [y/N]: " SETUP_GMAIL
if [[ "${SETUP_GMAIL:-n}" =~ ^[Yy] ]]; then
    echo -e "  ${ARROW} Installing Google Workspace MCP..."
    npm install -g google-workspace-mcp 2>/dev/null && echo -e "  ${CHECK} Google Workspace MCP installed"
    mkdir -p "$HOME/.google-mcp"
    echo -e "  ${ARROW} Follow setup: ${GREEN}npx google-workspace-mcp setup${NC}"
fi

echo ""

# ─── Memory system ───
echo -e "${BOLD}Setting up AURA memory...${NC}"
mkdir -p "$HOME/.aura/memory" "$HOME/.aura/context"

if [ ! -f "$HOME/.aura/memory/MEMORY.md" ]; then
    cat > "$HOME/.aura/memory/MEMORY.md" << 'MEMEOF'
# AURA Memory

## Owner
- Name: (your name)
- Telegram ID: (your ID)

## Preferences
- (AURA will learn your preferences over time)

## Projects
- (AURA will track your projects here)
MEMEOF
    echo -e "  ${CHECK} Memory initialized"
else
    echo -e "  ${CHECK} Memory already exists"
fi

echo ""

# ─── LaunchAgent (macOS) ───
if [[ "$(uname)" == "Darwin" ]]; then
    echo -e "${BOLD}Setting up auto-start (macOS)...${NC}"

    PLIST_PATH="$HOME/Library/LaunchAgents/com.aura.telegram-bot.plist"
    cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aura.telegram-bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/bin/claude-telegram-bot</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/logs/bot.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/logs/bot.stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
PLISTEOF

    mkdir -p "$INSTALL_DIR/logs"
    launchctl load "$PLIST_PATH" 2>/dev/null || true
    echo -e "  ${CHECK} Auto-start configured"
fi

echo ""

# ─── Summary ───
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  AURA installed successfully! 🚀${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo ""
echo -e "  Brains enabled: ${BOLD}${BRAINS[*]}${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo -e "  1. Authenticate your brains (see above)"
echo -e "  2. Open Telegram → message your bot"
echo -e "  3. Send /brains to see status"
echo ""
echo -e "  ${BOLD}Commands:${NC}"
echo -e "  /brain claude  — switch to Claude"
echo -e "  /brain codex   — switch to Codex"
echo -e "  /brain gemini  — switch to Gemini"
echo -e "  /brains        — show all brain status"
echo -e "  !command       — run shell directly (zero tokens)"
echo ""
echo -e "  ${CYAN}Docs: https://github.com/royaluniondesign-sys/aura${NC}"
echo ""
