#!/usr/bin/env bash
#
# AURA SuperNode Setup — Run on any machine to join the fleet
#
# Usage from primary AURA (Telegram):
#   /ssh target "curl -fsSL https://raw.githubusercontent.com/royaluniondesign-sys/aura/main/scripts/setup-node.sh | bash"
#
# Or run directly on the target machine:
#   curl -fsSL https://raw.githubusercontent.com/royaluniondesign-sys/aura/main/scripts/setup-node.sh | bash
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'
CHECK="${GREEN}✓${NC}"
ARROW="${CYAN}→${NC}"

echo ""
echo -e "${BOLD}AURA SuperNode Setup${NC}"
echo -e "${CYAN}Making this machine part of the AURA fleet${NC}"
echo ""

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

echo -e "${BOLD}System:${NC} $OS ($ARCH)"

# ─── Detect capabilities ───
echo -e "\n${BOLD}Detecting capabilities...${NC}"

# RAM
if [ "$OS" = "darwin" ]; then
    RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
    RAM_GB=$(( RAM_BYTES / 1024 / 1024 / 1024 ))
elif [ -f /proc/meminfo ]; then
    RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    RAM_GB=$(( RAM_KB / 1024 / 1024 ))
else
    RAM_GB=8
fi
echo -e "  ${CHECK} RAM: ${RAM_GB}GB"

# CPU
CPU_CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
echo -e "  ${CHECK} CPU: ${CPU_CORES} cores"

# GPU
HAS_GPU=false
GPU_INFO="none"
if nvidia-smi >/dev/null 2>&1; then
    HAS_GPU=true
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "NVIDIA detected")
    echo -e "  ${CHECK} GPU: $GPU_INFO"
elif system_profiler SPDisplaysDataType 2>/dev/null | grep -q "Metal"; then
    echo -e "  ${CHECK} GPU: Apple Metal (integrated)"
else
    echo -e "  ${ARROW} GPU: not detected"
fi

# Tools
echo -e "\n${BOLD}Available tools:${NC}"
TOOLS=()
for tool in python3 node npm git docker ffmpeg gcc cargo go java ruby curl wget tmux; do
    if command -v "$tool" >/dev/null 2>&1; then
        ver=$($tool --version 2>/dev/null | head -1 | cut -d' ' -f2-3 || echo "installed")
        echo -e "  ${CHECK} $tool ($ver)"
        TOOLS+=("$tool")
    fi
done

# ─── Install essentials ───
echo -e "\n${BOLD}Installing essentials...${NC}"

# Python
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "  ${ARROW} Python3 not found. Please install Python 3.11+."
fi

# uv
if ! command -v uv >/dev/null 2>&1; then
    echo -e "  ${ARROW} Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Node (for CLI tools)
if ! command -v node >/dev/null 2>&1; then
    echo -e "  ${ARROW} Node.js not found. Brain CLIs need Node.js."
    echo -e "  ${ARROW} Install: https://nodejs.org or 'brew install node'"
fi

# ─── Install brain CLIs ───
echo -e "\n${BOLD}Installing brain CLIs...${NC}"

if command -v npm >/dev/null 2>&1; then
    # Claude
    if ! command -v claude >/dev/null 2>&1; then
        echo -e "  ${ARROW} Installing Claude CLI..."
        npm install -g @anthropic-ai/claude-code 2>/dev/null || echo -e "  ${RED}Claude install failed${NC}"
    fi
    echo -e "  ${CHECK} Claude: $(claude --version 2>/dev/null || echo 'run: claude auth login')"

    # Codex
    if ! command -v codex >/dev/null 2>&1; then
        echo -e "  ${ARROW} Installing Codex CLI..."
        npm install -g @openai/codex 2>/dev/null || echo -e "  ${RED}Codex install failed${NC}"
    fi
    echo -e "  ${CHECK} Codex: $(codex --version 2>/dev/null || echo 'run: codex login')"

    # Gemini
    if ! command -v gemini >/dev/null 2>&1; then
        echo -e "  ${ARROW} Installing Gemini CLI..."
        npm install -g @google/gemini-cli 2>/dev/null || echo -e "  ${RED}Gemini install failed${NC}"
    fi
    echo -e "  ${CHECK} Gemini: $(gemini --version 2>/dev/null || echo 'run: gemini')"
fi

# ─── Setup SSH ───
echo -e "\n${BOLD}SSH Configuration${NC}"
if [ ! -f "$HOME/.ssh/authorized_keys" ]; then
    echo -e "  ${ARROW} No authorized_keys found."
    echo -e "  ${ARROW} On your primary Mac, run:"
    echo -e "  ${GREEN}ssh-copy-id $(whoami)@$(hostname)${NC}"
else
    KEY_COUNT=$(wc -l < "$HOME/.ssh/authorized_keys" | tr -d ' ')
    echo -e "  ${CHECK} authorized_keys: $KEY_COUNT keys"
fi

# ─── Summary ───
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  SuperNode ready! 🚀${NC}"
echo -e "${BOLD}${GREEN}════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}Specs:${NC} ${RAM_GB}GB RAM · ${CPU_CORES} cores · GPU: ${HAS_GPU}"
echo -e "  ${BOLD}Tools:${NC} ${TOOLS[*]:-none}"
echo ""
echo -e "  ${BOLD}From your primary AURA (Telegram):${NC}"
echo -e "  /fleet add $(hostname | tr '[:upper:]' '[:lower:]') $(whoami)@$(hostname) \"$(hostname)\" $OS"
echo -e "  /nodes profile $(hostname | tr '[:upper:]' '[:lower:]')"
echo ""
echo -e "  ${BOLD}Authenticate brains on this machine:${NC}"
echo -e "  claude auth login"
echo -e "  codex login"
echo -e "  gemini    (opens browser)"
echo ""
