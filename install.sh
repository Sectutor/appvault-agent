#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AppVault Agent — One-Command VPS Installer
# Installs Docker (if needed), pulls the agent image, runs it.
# The agent phones home to the AppVault Cloud admin server.
#
# Usage:
#   curl -fsSL https://github.com/Sectutor/appvault-agent/releases/latest/download/install.sh | bash
#   # or with options:
#   CENTRAL_URL=https://appvault.airepoindex.com AGENT_NAME=my-server bash install.sh
#
# Env vars:
#   CENTRAL_URL  Admin server base URL (default: https://appvault.airepoindex.com)
#   AGENT_NAME   Display name for this agent (default: hostname)
#   AGENT_PORT   Local agent API port (default: 8086)
#   DATA_DIR     Persisted data dir (default: /opt/appvault-agent)
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────
CENTRAL_URL="${CENTRAL_URL:-https://appvault.airepoindex.com}"
AGENT_NAME="${AGENT_NAME:-$(hostname)}"
AGENT_PORT="${AGENT_PORT:-8086}"
DATA_DIR="${DATA_DIR:-/opt/appvault-agent}"
IMAGE="ghcr.io/sectutor/appvault-agent:latest"
CONTAINER_NAME="appvault-agent"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { echo -e "${GREEN}[agent]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
fail() { echo -e "${RED}[fail]${NC} $*"; exit 1; }

echo
say "╔══════════════════════════════════════════════╗"
say "║     AppVault Agent — VPS Installer          ║"
say "╚══════════════════════════════════════════════╝"
say "Central server : $CENTRAL_URL"
say "Agent name     : $AGENT_NAME"
say "Agent port     : $AGENT_PORT"
say "Data dir       : $DATA_DIR"
echo

# ── Root check ─────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || fail "Run as root: sudo bash install.sh"

# ── Step 1: Docker ─────────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  say "Docker already running."
elif command -v docker >/dev/null 2>&1; then
  warn "Docker CLI present but daemon not running — starting it..."
  systemctl start docker 2>/dev/null || service docker start 2>/dev/null || true
  sleep 3
else
  say "Installing Docker..."
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    curl -fsSL https://get.docker.com | sh
  elif command -v yum >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache docker && rc-update add docker default
  else
    fail "Unsupported package manager. Install Docker manually first."
  fi
  systemctl enable docker 2>/dev/null || true
  systemctl start docker 2>/dev/null || service docker start 2>/dev/null || true
  sleep 3
fi
docker info >/dev/null 2>&1 || fail "Docker daemon not reachable after install."

# ── Step 2: Data dir ───────────────────────────────────────────
mkdir -p "$DATA_DIR"
say "Data directory ready: $DATA_DIR"

# ── Step 3: Pull image ─────────────────────────────────────────
say "Pulling agent image: $IMAGE"
docker pull "$IMAGE"

# ── Step 4: Run agent ──────────────────────────────────────────
say "Removing old container (if any)..."
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

say "Starting AppVault Agent..."
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${AGENT_PORT}:8086" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "${DATA_DIR}:/data" \
  -e CENTRAL_URL="$CENTRAL_URL" \
  -e AGENT_NAME="$AGENT_NAME" \
  -e AGENT_PORT=8086 \
  -e POLL_INTERVAL=30 \
  -e HEARTBEAT_INTERVAL=60 \
  -e STORAGE_PATH=/data \
  "$IMAGE" >/dev/null

# ── Step 5: Verify ─────────────────────────────────────────────
say "Waiting for agent to start..."
sleep 5
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  say "✅ AppVault Agent is RUNNING"
  say "   Local UI : http://$(hostname -I 2>/dev/null | awk '{print $1}'):${AGENT_PORT}/"
  say "   Central  : $CENTRAL_URL"
  say "   Logs     : docker logs -f $CONTAINER_NAME"
  say "   It will auto-register with the admin panel within ~30s."
else
  fail "Container failed to start. Check: docker logs $CONTAINER_NAME"
fi
echo
