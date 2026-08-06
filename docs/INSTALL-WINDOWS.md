# AppVault for Windows — Requirements & Installation

## ✅ Requirements (check these first)

| Requirement | Details |
|---|---|
| **Windows version** | Windows 10 (build 2004 or newer) or Windows 11 |
| **Processor** | 64-bit (x64) — AMD or Intel |
| **Virtualization** | Must be **enabled in BIOS** (Intel: VT-x · AMD: SVM Mode) — Docker needs it |
| **RAM** | 8 GB recommended (4 GB minimum) |
| **Free disk space** | **At least 10 GB** (Docker Desktop + WSL + AppVault ≈ 4–6 GB, plus room for your apps) |
| **Docker Desktop** | **REQUIRED — install it first** (see Step 1 below) |
| **WSL2** | Required **by Docker Desktop** — installed and configured **automatically** (Docker Desktop and the AppVault installer both handle it). No separate action needed. Only prerequisite: virtualization enabled in BIOS (above) |
| **Internet** | Needed for the install (images are downloaded automatically) |
| **Administrator rights** | The installer needs to run as Administrator |

> 💡 Not sure if virtualization is on? Run the installer — it checks for you in Step 2 and tells you exactly what to enable if it's off.

---

## 📦 Step-by-step installation

### Step 1 — Install Docker Desktop (REQUIRED)
1. Download Docker Desktop from: **https://www.docker.com/products/docker-desktop/**
2. Run the installer and follow the defaults
3. **Launch Docker Desktop** from the Start menu
4. Accept the license agreement and **sign in** (a free Docker account — required on first launch)
5. Wait until the whale icon in the tray shows **"Engine running"** (it can take 1–2 minutes the first time)
   - If it asks about WSL updates — accept them
6. Verify in PowerShell:
   ```powershell
   docker --version
   ```
   You should see something like `Docker version 29.x.x`

### Step 2 — Install AppVault
1. Open **PowerShell** (right-click → "Run as Administrator")
2. Copy and paste this one command:
   ```powershell
   irm https://raw.githubusercontent.com/Sectutor/appvault-agent/main/install.ps1 | iex
   ```
3. Watch the steps run — **don't close the window**:
   - ✅ Checks virtualization, Windows version, and required features
   - ✅ Detects Docker Desktop
   - ✅ Starts the AppVault agent and store
4. When you see **"✅ AppVault is ready!"** — installation is complete

### Step 3 — Open your App Store
- Open your browser at: **http://localhost:8085/**
- You'll see the AppVault store with **10 free apps** you can install right away
- All other apps show a **🔒 Premium** badge

### Step 4 — Make your server invisible (recommended, 30 seconds)
1. Launch your first app
2. The store shows the **"🔒 Make your server invisible"** guide
3. Follow it to connect Tailscale — after that, only **you** can reach your apps

### Step 5 — Unlock everything (when you're ready)
- After paying, you'll receive a **license key**
- In the store: **Settings → License** → paste the key → **Apply License**
- All premium apps unlock

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| "Docker is not running" but the whale icon is there | Wait — the installer waits up to 5 minutes. Accept any dialog (license / WSL update / sign-in) that Docker Desktop shows |
| "Virtualization is disabled" | Reboot → enter BIOS (F2/Del/ESC) → enable Intel VT-x or AMD SVM → save & reboot → run the installer again |
| Docker installed but "not found" | Close PowerShell completely and reopen it, then run the command again |
| Installer window closes by itself | Use the latest version of the command (it no longer closes) — rerun it |
| Store shows an old/plain page | Rerun the installer — it refreshes the store UI automatically |

---

## 📁 What gets installed
- **AppVault Agent** — the engine that installs and runs your apps (port 8086)
- **App Store** — the dashboard you open at `http://localhost:8085/` (port 8085)
- Data lives in `C:\Users\<you>\.appvault\` — your apps and settings survive reinstalls

## 💾 Disk space tip
After a successful install you can reclaim space (optional):
```powershell
wsl --shutdown
docker system prune -a -f
```
Then in Docker Desktop: **Settings → Resources → Disk image size limit → 16 GB**.
