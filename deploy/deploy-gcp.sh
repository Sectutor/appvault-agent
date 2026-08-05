#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  AppVault — ONE-CLICK DEPLOY for Google Cloud (runs from Cloud Shell)
#  ------------------------------------------------------------------
#  Clicking "Install AppVault" on the website opens Cloud Shell with this
#  script. It does EVERYTHING:
#    1. opens the firewall for the store (tcp:8085)
#    2. creates a VM (Debian 13, full-access scopes, 20GB disk)
#    3. the VM installs AppVault automatically on first boot
#    4. waits, then prints your Store URL
#  No terminal knowledge, no SSH, no manual steps.
# ═══════════════════════════════════════════════════════════════════════
set -e

echo "🚀 AppVault one-click install — Google Cloud"
echo "   This takes about 4 minutes. Sit back."

PROJECT="$(gcloud config get-value project 2>/dev/null | tr -d '\n')"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  echo "❌ No Google Cloud project selected."
  echo "   In Cloud Shell:  gcloud config set project YOUR-PROJECT-ID"
  exit 1
fi
echo "   Project: $PROJECT"

ZONE="$(gcloud compute zones list --filter="status=UP AND region=us-central1" --limit=1 --format="value(name)" 2>/dev/null | head -1)"
[ -z "$ZONE" ] && ZONE="us-central1-a"
NAME="appvault-$(date +%m%d-%H%M)"

# ── 1. Firewall (user credentials — always works) ─────────────────────
echo "🔓 Opening the store port in the cloud firewall…"
gcloud compute firewall-rules create appvault-store \
  --allow tcp:8085 --source-ranges 0.0.0.0/0 \
  --description "AppVault store (bootstrap — locked down by Tailscale onboarding)" \
  2>/dev/null && echo "   firewall rule created" || echo "   firewall rule already exists"

# ── 2. VM with the installer baked in as a startup script ─────────────
echo "🖥️  Creating your AppVault server ($NAME)…"
gcloud compute instances create "$NAME" \
  --zone="$ZONE" \
  --machine-type=e2-medium \
  --boot-disk-size=20GB \
  --boot-disk-type=pd-standard \
  --image-family=debian-13 --image-project=debian-cloud \
  --scopes=cloud-platform \
  --metadata=startup-script='#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/Sectutor/appvault-agent/main/install.sh -o /root/appvault-install.sh
bash /root/appvault-install.sh > /root/appvault-install.log 2>&1
echo "INSTALL_DONE" >> /root/appvault-install.log'

IP="$(gcloud compute instances describe "$NAME" --zone="$ZONE" --format="value(networkInterfaces[0].accessConfigs[0].natIP)")"
echo "   Server created — installing AppVault (3-4 min)…"

# ── 3. Wait for the install to finish ─────────────────────────────────
for i in $(seq 1 40); do
  sleep 10
  DONE="$(gcloud compute ssh "$NAME" --zone="$ZONE" --command="grep -c INSTALL_DONE /root/appvault-install.log 2>/dev/null || true" --quiet 2>/dev/null | tr -d '\r' || true)"
  if [ "$DONE" = "1" ]; then
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "🎉  Your AppVault is ready!"
    echo ""
    echo "    Store:  http://${IP}:8085"
    echo ""
    echo "    Next: open the store, launch your first app, and follow"
    echo "    the on-screen steps to make your server invisible"
    echo "    (Tailscale — takes 30 seconds)."
    echo "════════════════════════════════════════════════════════════"
    exit 0
  fi
  echo "   ...still installing ($((i*10))s)"
done
echo "⚠️  Taking longer than expected. Check progress with:"
echo "   gcloud compute ssh $NAME --zone=$ZONE --command='tail -20 /root/appvault-install.log'"
