# AURA — Personal AI Agent
# Multi-stage build for minimal image size
#
# Build: docker build -t aura .
# Run:   docker compose up -d

FROM python:3.12-slim AS base

# System deps for faster-whisper (optional voice) and general use
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (for Claude/Codex/Gemini CLIs)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# ─── AI CLI Tools (install but don't auth — user does that) ───
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || true
RUN npm install -g @openai/codex 2>/dev/null || true
RUN npm install -g @google/gemini-cli 2>/dev/null || true

WORKDIR /app

# ─── Python dependencies ───
COPY pyproject.toml ./
RUN uv pip install --system -e . 2>/dev/null || pip install -e .

# ─── Application code ───
COPY src/ src/
COPY install.sh ./

# ─── AURA data directories ───
RUN mkdir -p /root/.aura/memory /root/.aura/context logs data

# ─── Default configuration ───
ENV PYTHONUNBUFFERED=1
ENV DASHBOARD_PORT=3000

EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:3000/api/health || exit 1

# ─── Entrypoint ───
CMD ["python", "-m", "src.main"]
