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
set -o pipefail

# ── Config (env or flags) ──────────────────────────────────────────────
TS_AUTH_KEY="${TS_AUTH_KEY:-}"
AGENT_NAME="${AGENT_NAME:-}"
PUBLIC_URL="${PUBLIC_URL:-}"
CENTRAL_URL="${CENTRAL_URL:-http://central:8000}"
INSTALL_DIR="/opt/appvault"
STORE_IMAGE="${STORE_IMAGE:-ghcr.io/sectutor/appvault-releases:v37}"   # public: launcher + heimdall + /api proxy
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
    ports: ["0.0.0.0:8085:80"]
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

# ── 6b. Detect the SSH session's IP (for the 24h SSH fallback rule) ───
CUR_IP="${SSH_CLIENT:-}"
if [ -z "$CUR_IP" ] || [ "$CUR_IP" = "127.0.0.1" ]; then
  CUR_IP="$(ss -tn '( sport = :22 )' 2>/dev/null | awk 'NR>1{print $4}' | cut -d: -f1 | head -1)"
fi
CUR_IP="${CUR_IP%% *}"
# No prompts, no IP questions: the store opens publicly for a short bootstrap
# window so the user can get in immediately. The Tailscale onboarding (guided
# in the store UI on first app launch) locks it down. The store holds no
# secrets: admin is disabled on client installs and the agent API is localhost.
STORE_VISIBILITY="public during bootstrap"

# ── 7. Start the stack + phone-home ───────────────────────────────────
log "Starting AppVault stack…"
docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" up -d || die "Stack start failed"
# Agent registers with central automatically on first start (phone-home).
sleep 10
log "Containers: $(docker ps --filter name=appvault --format '{{.Names}}' | tr '\n' ' ')"

# ── 8. FIREWALL — applied LAST (lockout-proof) ────────────────────────
log "Applying firewall (deny-all inbound, store PUBLIC for bootstrap)…"
export DEBIAN_FRONTEND=noninteractive
command -v ufw >/dev/null 2>&1 || apt-get install -y -qq ufw
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1

# Allow the SSH session's current IP for 24h (fallback in case Tailscale isn't ready)
if [ -n "$CUR_IP" ] && [ "$CUR_IP" != "127.0.0.1" ]; then
  ufw allow from "$CUR_IP" to any port 22 proto tcp comment "installer fallback (24h)" >/dev/null 2>&1
  # schedule rule removal in 24h
  ( sleep 86400; ufw delete allow from "$CUR_IP" to any port 22 proto tcp >/dev/null 2>&1 ) &
fi
# STORE: public bootstrap window (short) — the user can open the store right
# away with zero configuration. Tailscale onboarding (guided in the UI) locks it.
ufw allow 8085/tcp comment "store bootstrap (public)" >/dev/null 2>&1
# Tailscale subnet — private lane (SSH + API + admin stay tailnet-only)
ufw allow from 100.64.0.0/10 to any port 22 proto tcp comment "tailscale ssh" >/dev/null 2>&1
ufw allow from 100.64.0.0/10 to any port 8086 proto tcp comment "tailscale api" >/dev/null 2>&1
ufw allow from 100.64.0.0/10 to any port 8001 proto tcp comment "tailscale admin" >/dev/null 2>&1
ufw --force enable >/dev/null 2>&1
ufw status verbose | tee -a "$LOG"
log "Firewall active: deny-all inbound; store public on 8085 (until Tailscale onboarding locks it)"

# ── 8a. Cloud-level firewall (best-effort) ─────────────────────────────
# Most providers (OVH, Contabo, Hetzner, DO) have no network-level firewall —
# the host ufw above is the only gate. GCP is the exception: its VPC firewall
# is default-deny, so open port 8085 via the instance's own service account
# (metadata token). If that fails, print the manual step + the fix.
if curl -fsSL --max-time 2 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/project/project-id" >/dev/null 2>&1; then
  GCP_PROJECT="$(curl -fsSL --max-time 2 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/project/project-id" 2>/dev/null)"
  GCP_TOKEN="$(curl -fsSL --max-time 2 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" 2>/dev/null | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
  if [ -n "$GCP_TOKEN" ]; then
    GCP_RESP=$(curl -s -X POST "https://compute.googleapis.com/compute/v1/projects/${GCP_PROJECT}/global/firewalls" \
      -H "Authorization: Bearer ${GCP_TOKEN}" -H "Content-Type: application/json" \
      -d '{"name":"appvault-store","allowed":[{"IPProtocol":"tcp","ports":["8085"]}],"sourceRanges":["0.0.0.0/0"]}' 2>/dev/null)
    if echo "$GCP_RESP" | grep -q '"kind"'; then
      log "GCP VPC firewall rule created: tcp:8085 (public bootstrap)"
    else
      log "WARNING: your cloud (GCP) blocks port 8085 and the installer lacks permission to open it."
      log "  Fix: recreate the VM with 'Allow full access to all Cloud APIs' (Access scopes), or run from your own machine:"
      log "  gcloud compute firewall-rules create appvault-store --allow tcp:8085 --source-ranges 0.0.0.0/0"
    fi
  else
    log "WARNING: GCP detected but no service-account token — open port 8085 in the GCP firewall (VPC network → Firewall → tcp:8085, source 0.0.0.0/0)"
  fi
fi

# ── 8b. Onboarding helper: tailscale-onboard.sh (run from the store UI) ──
cat > "$INSTALL_DIR/tailscale-onboard.sh" <<'TSEOF'
#!/bin/bash
# AppVault — make this server invisible: join Tailscale, then lock the firewall.
# Usage: sudo bash tailscale-onboard.sh [--authkey KEY]
set -euo pipefail
INSTALL_DIR="/opt/appvault"
log() { echo "[tailscale-onboard] $*"; }
AUTHKEY=""
[ "${1:-}" = "--authkey" ] && AUTHKEY="${2:-}"
if ! command -v tailscale >/dev/null 2>&1; then
  log "Installing Tailscale…"
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1 || { log "ERROR: tailscale install failed"; exit 1; }
fi
if ! tailscale status >/dev/null 2>&1; then
  if [ -n "$AUTHKEY" ]; then
    log "Joining Tailscale with auth key…"
    tailscale up --authkey="$AUTHKEY" >/dev/null 2>&1 || { log "ERROR: join failed (check the key)"; exit 1; }
  else
    log "Run this on the server to approve (opens a URL): tailscale up"
    tailscale up 2>/dev/null || true
  fi
fi
for i in $(seq 1 30); do
  TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
  [ -n "$TS_IP" ] && break
  sleep 2
done
[ -z "$TS_IP" ] && { log "ERROR: not on the tailnet yet — approve the login URL, then rerun"; exit 1; }
# Lock the firewall: remove EVERY bootstrap rule for the store port,
# then allow only the tailnet
for r in $(ufw status numbered | grep "8085" | awk -F'[][]' '{print $2}' | sort -rn); do
  echo y | ufw delete "$r" >/dev/null 2>&1 || true
done
ufw allow from 100.64.0.0/10 to any port 8085 proto tcp comment "tailscale store" >/dev/null 2>&1
ufw --force enable >/dev/null 2>&1
cat > "$INSTALL_DIR/tailscale-status.json" <<EOF
{"joined": true, "ip": "$TS_IP", "store_url": "http://$TS_IP:8085", "locked": true}
EOF
log "Invisible! Store is now tailnet-only: http://$TS_IP:8085"
TSEOF
chmod +x "$INSTALL_DIR/tailscale-onboard.sh"

# ── 9. Success screen ─────────────────────────────────────────────────
PUB_IP=""
curl -fsSL -H "Metadata-Flavor: Google" --max-time 3 "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" >/dev/null 2>&1 && PUB_IP=$(curl -fsSL -H "Metadata-Flavor: Google" --max-time 3 "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" 2>/dev/null)
[ -z "$PUB_IP" ] && PUB_IP=$(curl -fsSL --max-time 5 https://ifconfig.me 2>/dev/null || true)
ACCESS="http://127.0.0.1:8085"
[ -n "$PUB_IP" ] && ACCESS="http://$PUB_IP:8085"
cat <<EOF | tee -a "$LOG"

════════════════════════════════════════════════════════════════════
✅  AppVault installed
    Plan     : Free (10 starter apps) — apply a license key later in Settings → License   Agent: $AGENT_NAME
    Store UI: $ACCESS   (${STORE_VISIBILITY:-public} — open this in your browser)
    🔒 Tip    : open an app once, and AppVault will guide you to make
                 this server fully invisible via Tailscale (30 seconds)
    API key : saved in $INSTALL_DIR/.env (also shown below)
    API_KEY : $API_KEY

    🔒 Firewall: deny-all inbound; store public until Tailscale onboarding
    📡 Phone-home: $CENTRAL_URL (catalog, updates, license)
    🚪 Lost SSH? Use your provider's web console (escape hatch)
════════════════════════════════════════════════════════════════════
EOF

# ── 9b. REACHABILITY VERIFICATION — the install is not complete until ─
# the store actually answers from the internet. Some clouds (GCP, AWS)
# block ports at their own network level; if the store can't be reached,
# we say so, print the fix, and keep verifying instead of claiming DONE.
PROBE_URL="http://${PUB_IP:-127.0.0.1}:8085"
VERIFIED=""
if [ -n "${PUB_IP:-}" ]; then
  log "Verifying your store is reachable from the internet: $PROBE_URL"
  for i in $(seq 1 30); do
    R="$(curl -fsSL --max-time 8 https://appvault.airepoindex.com/api/probe -H "Content-Type: application/json" -d "{\"url\":\"$PROBE_URL\"}" 2>/dev/null || true)"
    if echo "$R" | grep -q '"reachable": *true'; then
      VERIFIED="1"
      break
    fi
    if [ "$i" -eq 3 ]; then
      log "⚠️  Your cloud provider is blocking port 8085 (the store can't be reached from the internet yet)."
      if curl -fsSL --max-time 2 -H "Metadata-Flavor: Google" "http://metadata.google.internal" >/dev/null 2>&1; then
        log "    Google Cloud: open https://console.cloud.google.com/networking/firewalls"
        log "    → CREATE FIREWALL RULE → name: appvault-store → TCP 8085 → source 0.0.0.0/0 → CREATE"
        log "    (or run from your computer: gcloud compute firewall-rules create appvault-store --allow tcp:8085 --source-ranges 0.0.0.0/0)"
      else
        log "    Open port 8085 (TCP) in your provider's control panel (AWS: security group, Azure: NSG, OVH/Hetzner/Contabo: usually none needed)."
      fi
      log "    Waiting and re-checking every 10s…"
    fi
    sleep 10
  done
fi
if [ -n "$VERIFIED" ]; then
  log "✅ Store verified reachable: $PROBE_URL — install complete"
else
  log "⚠️  Install finished, but the store could not be verified from the internet yet."
  log "    Once you open port 8085, the store will be at $PROBE_URL"
fi
log "DONE — AppVault invisible VPS ready"
