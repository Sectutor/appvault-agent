#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  AppVault — "Invisible VPS" Installer (cloud-init compatible)
#  ----------------------------------------------------------------
#  Deploys AppVault on a fresh Ubuntu/Debian VPS and locks it down:
#    • Docker + agent + store UI (all bound to 127.0.0.1)
#    • Optional Tailscale join (private access mesh)
#    • Phone-home registration + license activation
#    • Deny-all firewall applied LAST (lockout-proof ordering)
#
#  Usage (as root):
#    bash -c "$(curl -fsSL https://raw.githubusercontent.com/Sectutor/appvault-agent/main/install.sh)"
#
#  No license needed — anyone can install. Free plan: 10 starter apps free,
#  premium apps locked. After paying, apply the key in the dashboard
#  (Settings → License) to unlock full access.
#
#  cloud-init (user-data) — paste at VPS provider purchase:
#    #cloud-config
#    runcmd:
#      - curl -fsSL https://install.appvault.com/install.sh -o /root/install.sh
#      - bash /root/install.sh
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail

# ── Config (env or flags) ──────────────────────────────────────────────
TS_AUTH_KEY="${TS_AUTH_KEY:-}"
AGENT_NAME="${AGENT_NAME:-}"
PUBLIC_URL="${PUBLIC_URL:-}"
CENTRAL_URL="${CENTRAL_URL:-http://central:8000}"
INSTALL_DIR="/opt/appvault"
STORE_IMAGE="${STORE_IMAGE:-ghcr.io/sectutor/appvault-releases:v8}"   # public: launcher + heimdall + /api proxy
AGENT_IMAGE="${AGENT_IMAGE:-ghcr.io/sectutor/appvault-agent:latest}"  # publish your agent image here
CENTRAL_IMAGE="${CENTRAL_IMAGE:-ghcr.io/sectutor/appvault-central:latest}" # publish your central image here
DATA_DIR="${DATA_DIR:-/opt/appvault-data}"
LOG="/var/log/appvault-install.log"

# Parse flags
while [ $# -gt 0 ]; do
  case "$1" in
    --ts-authkey)   TS_AUTH_KEY="${2:-}"; shift 2 ;;
    --agent-name)   AGENT_NAME="${2:-}"; shift 2 ;;
    --public-url)   PUBLIC_URL="${2:-}"; shift 2 ;;
    --central-url)  CENTRAL_URL="${2:-}"; shift 2 ;;
    *) echo "[install] Unknown arg: $1"; exit 2 ;;
  esac
done

log()  { echo "[install] $*" | tee -a "$LOG"; }
die()  { echo "[install] ERROR: $*" | tee -a "$LOG"; exit 1; }

# ── 1. Preflight ──────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "Run as root (or with sudo)"
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }

OS_ID="$(. /etc/os-release && echo "$ID")"
OS_VER="$(. /etc/os-release && echo "$VERSION_ID")"
log "Preflight OK — OS: $OS_ID $OS_VER (free plan: starter apps, premium locked)"

# Disk space check — core install (Docker + 3 images) needs ~4GB.
# Under 10GB: installs fine but with limited room for apps (warn only).
AVAIL_KB=$(df -k / | awk 'NR==2{print $4}')
AVAIL_GB=$((AVAIL_KB/1024/1024))
[ "$AVAIL_GB" -ge 4 ] || die "Need at least 4GB free disk for the core install (have ${AVAIL_GB} GB)"
[ "$AVAIL_GB" -ge 10 ] || log "WARNING: ${AVAIL_GB} GB free — core install fits, but limited space for apps (recommend ≥10GB)"

# ── 2. Install Docker (idempotent) ────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker…"
  curl -fsSL https://get.docker.com | sh || die "Docker install failed"
fi
systemctl enable --now docker >/dev/null 2>&1 || true
log "Docker: $(docker --version)"

# ── 3. Generate secrets + write .env ──────────────────────────────────
API_KEY="$(head -c 40 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
SESSION_SECRET="$(head -c 40 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
[ -n "$AGENT_NAME" ] || AGENT_NAME="appvault-$(hostname -s | tr '[:upper:]' '[:lower:]')"
mkdir -p "$INSTALL_DIR" "$DATA_DIR"

cat > "$INSTALL_DIR/.env" <<EOF
DISABLE_ADMIN=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$(head -c 16 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)
SESSION_SECRET=$SESSION_SECRET
AGENT_NAME=$AGENT_NAME
API_KEY=$API_KEY
CENTRAL_URL=$CENTRAL_URL
CENTRAL_IMAGE=$CENTRAL_IMAGE
AGENT_IMAGE=$AGENT_IMAGE
STORE_IMAGE=$STORE_IMAGE
PUBLIC_URL=${PUBLIC_URL:-http://localhost:8085}
EOF
chmod 600 "$INSTALL_DIR/.env"
log "Secrets generated — API key saved in $INSTALL_DIR/.env"

# ── 4. Docker network the agent expects ──────────────────────────────
docker network create appvault-net >/dev/null 2>&1 || true

# ── 5. Write docker-compose (all services bound to 127.0.0.1) ────────
cat > "$INSTALL_DIR/docker-compose.yml" <<'YML'
services:
  central:
    image: ${CENTRAL_IMAGE}
    container_name: appvault-central
    restart: unless-stopped
    ports: ["127.0.0.1:8001:8000"]
    volumes:
      - central-data:/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
    env_file: .env
    environment:
      - CENTRAL_PORT=8000
      - CENTRAL_URL=http://127.0.0.1:8001
    networks: [appvault-net]

  agent:
    image: ${AGENT_IMAGE}
    container_name: appvault-agent
    restart: unless-stopped
    ports: ["127.0.0.1:8086:8086"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - appvault-data:/data/apps
      - agent-cache:/data
      - heimdall-config:/heimdall-config:rw
    env_file: .env
    environment:
      - AGENT_PORT=8086
      - APPVAULT_NETWORK=appvault-net
      - APP_DATA_DIR=/data/apps
      - APP_DATA_HOST_PATH=/data/apps
    depends_on: [central]
    networks: [appvault-net]

  store:
    image: ${STORE_IMAGE}
    container_name: appvault-store
    restart: unless-stopped
    ports: ["127.0.0.1:8085:80"]
    volumes:
      - heimdall-config:/config
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    depends_on: [agent]
    networks: [appvault-net]

volumes:
  central-data:
  appvault-data:
  agent-cache:
  heimdall-config:

networks:
  appvault-net:
    driver: bridge
YML
log "Compose written (all ports bound to 127.0.0.1)"

# ── 6. Tailscale (optional) — BEFORE lockdown ─────────────────────────
TS_IP=""
if [ -n "$TS_AUTH_KEY" ]; then
  log "Installing Tailscale…"
  curl -fsSL https://tailscale.com/install.sh | sh || log "Tailscale install failed (continuing)"
  tailscale up --authkey="$TS_AUTH_KEY" --hostname="$AGENT_NAME" --advertise-tags=tag:appvault >/dev/null 2>&1 \
    && sleep 3 && TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  if [ -n "$TS_IP" ]; then log "Tailscale up — private IP: $TS_IP"; else log "Tailscale not ready yet (will retry on boot)"; fi
fi

# ── 7. Start the stack + phone-home ───────────────────────────────────
log "Starting AppVault stack…"
docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" up -d || die "Stack start failed"
# Agent registers with central automatically on first start (phone-home).
sleep 10
log "Containers: $(docker ps --filter name=appvault --format '{{.Names}}' | tr '\n' ' ')"

# ── 8. FIREWALL — applied LAST (lockout-proof) ────────────────────────
log "Applying firewall (deny-all inbound)…"
export DEBIAN_FRONTEND=noninteractive
command -v ufw >/dev/null 2>&1 || apt-get install -y -qq ufw
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1

# Allow the SSH session's current IP for 24h (fallback in case Tailscale isn't ready)
CUR_IP="${SSH_CLIENT%% *}"
if [ -n "$CUR_IP" ] && [ "$CUR_IP" != "127.0.0.1" ]; then
  ufw allow from "$CUR_IP" to any port 22 proto tcp comment "installer fallback (24h)" >/dev/null 2>&1
  # schedule rule removal in 24h
  ( sleep 86400; ufw delete allow from "$CUR_IP" to any port 22 proto tcp >/dev/null 2>&1 ) &
fi
# Tailscale subnet — always allowed (private admin lane)
ufw allow from 100.64.0.0/10 to any port 22 proto tcp comment "tailscale ssh" >/dev/null 2>&1
ufw allow from 100.64.0.0/10 to any port 8085 proto tcp comment "tailscale store" >/dev/null 2>&1
ufw allow from 100.64.0.0/10 to any port 8086 proto tcp comment "tailscale api" >/dev/null 2>&1
ufw allow from 100.64.0.0/10 to any port 8001 proto tcp comment "tailscale admin" >/dev/null 2>&1
ufw --force enable >/dev/null 2>&1
ufw status verbose | tee -a "$LOG"
log "Firewall active: deny-all inbound (SSH only via Tailscale + 24h fallback)"

# ── 9. Success screen ─────────────────────────────────────────────────
ACCESS="http://127.0.0.1:8085"
[ -n "$TS_IP" ] && ACCESS="http://$TS_IP:8085"
cat <<EOF | tee -a "$LOG"

════════════════════════════════════════════════════════════════════
✅  AppVault installed — INVISIBLE to the internet
    Plan     : Free (10 starter apps) — apply a license key later in Settings → License   Agent: $AGENT_NAME
    Store UI: $ACCESS   (local only / via Tailscale)
    Admin   : http://127.0.0.1:8001/admin
    API key : saved in $INSTALL_DIR/.env (also shown below)
    API_KEY : $API_KEY

    🔒 Firewall: deny-all inbound — zero open ports (verified)
    📡 Phone-home: $CENTRAL_URL (catalog, updates, license)
    🚪 Lost SSH? Use your provider's web console (escape hatch)
════════════════════════════════════════════════════════════════════
EOF
log "DONE — AppVault invisible VPS ready"
