<#
.AppVault Installer for Windows
Detects, validates, and installs everything needed to run AppVault locally.
Usage: irm https://appvault.airepoindex.com/install.ps1 | iex
#>

$ErrorActionPreference = "Stop"
$STEP = 0

function Step($msg) {
    $global:STEP++
    Write-Host "`n[$STEP] $msg" -ForegroundColor Cyan
}

function Success($msg) {
    Write-Host "  ✅ $msg" -ForegroundColor Green
}

function Warn($msg) {
    Write-Host "  ⚠️  $msg" -ForegroundColor Yellow
}

function Fail($msg) {
    Write-Host "  ❌ $msg" -ForegroundColor Red
    # throw (not exit) — exit would close the user's PowerShell window
    throw $msg
}

function CheckAdmin() {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        Fail "Administrator rights required. Right-click PowerShell and 'Run as Administrator'."
    }
    Success "Administrator rights confirmed"
}

# ═══════════════════════════════════════════
# STEP 1: Admin check
# ═══════════════════════════════════════════
Clear-Host
Write-Host "⚡ AppVault Installer for Windows" -ForegroundColor Cyan
Write-Host "=================================="
CheckAdmin

# ═══════════════════════════════════════════
# STEP 2: CPU virtualization check
# ═══════════════════════════════════════════
Step "Checking CPU virtualization support"
$cpu = Get-CimInstance Win32_Processor
$cpuName = $cpu.Name
$hasVT = $cpu.VirtualizationFirmwareEnabled

Write-Host "  CPU: $cpuName"
if ($hasVT) {
    Success "Virtualization (VT-x/AMD-V) is enabled in BIOS"
} else {
    Warn "Virtualization appears disabled in BIOS."
    Warn "  → For Intel: Enable 'Intel Virtualization Technology (VT-x)' in BIOS"
    Warn "  → For AMD: Enable 'SVM Mode' in BIOS"
    Warn "  → Reboot, press F2/Del/ESC during startup to enter BIOS"
    Warn "  → After enabling, run this installer again"
    $continue = Read-Host "Do you want to continue anyway? (y/N)"
    if ($continue -ne "y") { return }
}

# ═══════════════════════════════════════════
# STEP 3: Check OS version + WSL support
# ═══════════════════════════════════════════
Step "Checking Windows version"
$os = Get-CimInstance Win32_OperatingSystem
$ver = [System.Environment]::OSVersion.Version
$build = $ver.Build

if ($build -ge 19041) {
    Success "Windows 10 2004+ (build $build) — WSL2 supported"
} elseif ($build -ge 18362) {
    Warn "Windows 10 1903/1909 (build $build) — WSL2 requires manual update"
} else {
    Fail "Windows version too old (build $build). Need Windows 10 2004+"
}

# ═══════════════════════════════════════════
# STEP 4: Check/Install WSL2 + Virtual Machine Platform
# ═══════════════════════════════════════════
Step "Checking Windows Features (WSL2 + Virtual Machine Platform)"
$needsReboot = $false

$features = @(
    @{Name="Microsoft-Windows-Subsystem-Linux"; Label="Windows Subsystem for Linux"},
    @{Name="VirtualMachinePlatform"; Label="Virtual Machine Platform"},
    @{Name="Microsoft-Hyper-V"; Label="Hyper-V"}
)

foreach ($f in $features) {
    # Use dism.exe — Get-WindowsOptionalFeature is Windows PowerShell 5.1 only
    # and fails with "Class not registered" under PowerShell 7.
    $out = & dism.exe /online /Get-FeatureInfo /FeatureName:$($f.Name) 2>$null | Out-String
    if ($out -match "State\s*:\s*Enabled") {
        Success "$($f.Label) — already enabled"
    } else {
        Warn "$($f.Label) — not enabled, installing..."
        & dism.exe /online /Enable-Feature /FeatureName:$($f.Name) /All /NoRestart 2>$null | Out-Null
        $needsReboot = $true
        Success "$($f.Label) — installed (reboot pending)"
    }
}

# ═══════════════════════════════════════════
# STEP 5: Install WSL kernel update if needed
# ═══════════════════════════════════════════
Step "Setting WSL2 as default"
wsl --set-default-version 2 2>$null
if ($LASTEXITCODE -eq 0) {
    Success "WSL2 set as default"
} else {
    Warn "WSL kernel update may be needed."
    Warn "  Download: https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi"
    Warn "  Install the .msi, then run: wsl --set-default-version 2"
}

# ═══════════════════════════════════════════
# STEP 6: Check if reboot needed
# ═══════════════════════════════════════════
if ($needsReboot) {
    Warn "Windows features were installed — a reboot is required."
    $rebootNow = Read-Host "Reboot now? (Y/n)"
    if ($rebootNow -ne "n") {
        Restart-Computer -Confirm:$false
        return
    }
    Write-Host "`nAfter reboot, run this installer again to continue."
    return
}

# ═══════════════════════════════════════════
# STEP 7: Check/Install Docker Desktop
# ═══════════════════════════════════════════
Step "Checking Docker Desktop"
# Refresh PATH so a fresh Docker Desktop install is visible in THIS session
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$dockerExe = "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe"
$dockerDesktopExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
$dockerExists = (Test-Path $dockerExe) -or (Get-Command docker -ErrorAction SilentlyContinue)

if ($dockerExists) {
    $docker = if (Test-Path $dockerExe) { $dockerExe } else { "docker" }
    try { $version = & $docker --version 2>$null } catch { $version = "docker CLI found" }
    Success "Docker already installed: $version"
} else {
    Warn "Docker Desktop not found — downloading..."
    
    # Detect architecture
    if ([Environment]::Is64BitOperatingSystem) {
        $url = "https://desktop.docker.com/win/stable/amd64/Docker%20Desktop%20Installer.exe"
    } else {
        Fail "Docker Desktop requires a 64-bit operating system"
    }
    
    $installer = "$env:TEMP\DockerDesktopInstaller.exe"
    Write-Host "  Downloading (250MB)..."
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    
    Write-Host "  Installing Docker Desktop (may take 5 minutes)..."
    Start-Process $installer -Wait -ArgumentList "install", "--quiet"
    
    # Verify installation (PATH refresh again — the installer updates PATH)
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    if (Test-Path $dockerExe) {
        $docker = $dockerExe
        Success "Docker Desktop installed: $(& $docker --version)"
    } elseif (Get-Command docker -ErrorAction SilentlyContinue) {
        $docker = "docker"
        Success "Docker Desktop installed: $(docker --version)"
    } else {
        Warn "Docker Desktop installer may still be running in background."
        Warn "  After it finishes, Docker will appear in your Start menu."
    }
}

# ═══════════════════════════════════════════
# STEP 8: Start Docker if not running
# ═══════════════════════════════════════════
Step "Starting Docker"
$dockerOK = $false
try {
    $info = & $docker info 2>&1
    $dockerOK = $LASTEXITCODE -eq 0
} catch {}

if (-not $dockerOK) {
    Warn "Docker is not running — starting Docker Desktop..."
    if (-not (Test-Path $dockerDesktopExe)) {
        $dockerDesktopExe = (Get-ItemProperty "HKLM:\SOFTWARE\Docker Inc.\Docker Desktop" -ErrorAction SilentlyContinue).AppPath
    }
    if (Test-Path $dockerDesktopExe) {
        Start-Process $dockerDesktopExe
    } else {
        Warn "  Docker Desktop.exe not found — please start it from the Start menu."
        Start-Process "shell:AppsFolder\Docker Desktop" -ErrorAction SilentlyContinue
    }
    Write-Host "  Waiting for Docker to start (up to 180 seconds)..."
    Write-Host "  ⚠️  If Docker Desktop shows 'Docker Desktop requires a newer WSL kernel' or a login prompt,"
    Write-Host "     complete those dialogs — the installer waits for the engine."
    
    $maxWait = 180
    $waited = 0
    while ($waited -lt $maxWait) {
        try {
            & $docker info 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
        } catch {}
        Start-Sleep -Seconds 5
        $waited += 5
        Write-Host "  ... still waiting ($waited seconds)"
    }
}

try {
    & $docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Success "Docker is running"
    } else {
        Fail "Docker failed to start. Launch Docker Desktop manually, accept the license, and rerun this installer."
    }
} catch {
    Fail "Docker failed to start. Launch Docker Desktop manually, accept the license, and rerun this installer."
}

# ═══════════════════════════════════════════
# STEP 9: Pull AppVault Agent and start
# ═══════════════════════════════════════════
Step "Starting AppVault Agent"
# Shared API key for agent + store proxy
$apiKey = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
Write-Host "  Pulling AppVault images..."
& $docker pull ghcr.io/sectutor/appvault-agent:latest 2>&1 | Out-Null
& $docker pull ghcr.io/sectutor/appvault-releases:v10 2>&1 | Out-Null

# Create data directory
mkdir "$env:USERPROFILE\.appvault\data" -Force | Out-Null
mkdir "$env:USERPROFILE\.appvault\apps" -Force | Out-Null

# Stop any existing agent
& $docker stop appvault-agent 2>$null | Out-Null
& $docker rm appvault-agent 2>$null | Out-Null

# Start agent
Write-Host "  Starting AppVault Agent on port 8086..."
& $docker run -d `
  --name appvault-agent `
  --restart unless-stopped `
  -p 8086:8086 `
  -v /var/run/docker.sock:/var/run/docker.sock:ro `
  -v "$env:USERPROFILE\.appvault\data:/data" `
  -v "$env:USERPROFILE\.appvault\apps:/data/apps" `
  -e AGENT_PORT=8086 `
  -e API_KEY=$apiKey `
  -e CENTRAL_URL=https://appvault.airepoindex.com `
  -e AGENT_NAME="$env:COMPUTERNAME-agent" `
  -e STORAGE_PATH=/data `
  ghcr.io/sectutor/appvault-agent:latest

# Start Heimdall
Write-Host "  Starting App Store on port 8085..."
& $docker stop appvault-heimdall 2>$null | Out-Null
& $docker rm appvault-heimdall 2>$null | Out-Null

# Clear stale UI files — the store image re-seeds /config/www on first boot
Remove-Item "$env:USERPROFILE\.appvault\heimdall-config\www" -Recurse -Force -ErrorAction SilentlyContinue

& $docker run -d `
  --name appvault-heimdall `
  --restart unless-stopped `
  -p 8085:80 `
  -v "$env:USERPROFILE\.appvault\heimdall-config:/config" `
  -e API_KEY=$apiKey `
  -e CENTRAL_URL=https://appvault.airepoindex.com `
  -e PUID=1000 `
  -e PGID=1000 `
  -e TZ=Etc/UTC `
  ghcr.io/sectutor/appvault-releases:v10

# ═══════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════
Write-Host "`n" -NoNewline
Write-Host "==================================" -ForegroundColor Green
Write-Host "✅ AppVault is ready!" -ForegroundColor Green
Write-Host "==================================" -ForegroundColor Green
Write-Host "`n"
Write-Host "  📦 App Store:  http://localhost:8085/" -ForegroundColor Cyan
Write-Host "  ⚙️  Dashboard:  http://localhost:8085/index.php" -ForegroundColor Cyan
Write-Host "`n"
Write-Host "  Press any key to open the App Store..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
Start-Process "http://localhost:8085/"
