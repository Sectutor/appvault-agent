# ==============================================================================
# AppVault Installer for Windows
# Detects, validates, and installs everything needed to run AppVault locally.
# Usage: irm https://appvault.airepoindex.com/install.ps1 | iex
# ==============================================================================

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 defaults to TLS 1.0/1.1, which modern HTTPS servers reject.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
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
        Write-Host "  🔄 Requesting administrator privileges (UAC prompt)..." -ForegroundColor Yellow
        $url = "https://appvault.airepoindex.com/install.ps1"
        $cmd = "try { iex (irm '$url') } catch { Write-Host '  ❌ Install failed in elevated session.' -ForegroundColor Red; Read-Host 'Press Enter to close' }"
        try {
            Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd)
            exit
        } catch {
            Fail "Administrator rights required. Right-click PowerShell and 'Run as Administrator'."
        }
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
    try { $out = & dism.exe /online /Get-FeatureInfo /FeatureName:$($f.Name) 2>$null | Out-String } catch { $out = "" }
    if ($out -match "State\s*:\s*Enabled") {
        Success "$($f.Label) — already enabled"
    } else {
        Warn "$($f.Label) — not enabled, installing..."
        try { & dism.exe /online /Enable-Feature /FeatureName:$($f.Name) /All /NoRestart 2>$null | Out-Null } catch {}
        $needsReboot = $true
        Success "$($f.Label) — installed (reboot pending)"
    }
}

# ═══════════════════════════════════════════
# STEP 5: Install WSL kernel update if needed
# ═══════════════════════════════════════════
Step "Setting WSL2 as default and RAM safety limits"
try {
    wsl --set-default-version 2 2>$null
    $wslDefault2 = ($LASTEXITCODE -eq 0)
} catch {
    # PS 5.1 turns redirected native stderr into a terminating error
    $wslDefault2 = $false
}
if ($wslDefault2) {
    Success "WSL2 set as default"
} else {
    Warn "WSL kernel update may be needed."
    Warn "  Download: https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi"
    Warn "  Install the .msi, then run: wsl --set-default-version 2"
}

# Create .wslconfig RAM cap if not present (prevents WSL2 VMMEM from consuming 100% host RAM)
$wslConfigFile = "$env:USERPROFILE\.wslconfig"
if (-not (Test-Path $wslConfigFile)) {
    "[wsl2]`nmemory=6GB`nprocessors=4" | Out-File -Encoding ascii $wslConfigFile
    Success "Created .wslconfig (capped WSL2 memory to 6GB to protect host RAM)"
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
# Find Docker wherever it lives: machine install, per-user install, or PATH
$dockerCandidates = @(
    "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
    "$env:LOCALAPPDATA\Docker\Docker\resources\bin\docker.exe",
    (Get-Command docker -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path $_) }
$docker = if ($dockerCandidates) { @($dockerCandidates)[0] } else { "docker" }
$dockerDesktopExe = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
    "$env:LOCALAPPDATA\Docker\Docker\Docker Desktop.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$dockerExists = [bool]$dockerCandidates -or [bool](Get-Command docker -ErrorAction SilentlyContinue)

if ($dockerExists) {
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
    $dockerCandidates = @(
        "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
        "$env:LOCALAPPDATA\Docker\Docker\resources\bin\docker.exe",
        (Get-Command docker -ErrorAction SilentlyContinue).Source
    ) | Where-Object { $_ -and (Test-Path $_) }
    $docker = if ($dockerCandidates) { @($dockerCandidates)[0] } else { "docker" }
    if ($dockerCandidates) {
        Success "Docker Desktop installed: $(& $docker --version)"
    } else {
        Warn "Docker Desktop installer may still be running in background."
        Warn "  After it finishes, Docker will appear in your Start menu."
    }
}

# ═══════════════════════════════════════════
# STEP 8: Check Docker is accessible
# ═══════════════════════════════════════════
Step "Checking Docker"
$dockerOK = $false
try {
    $info = & $docker info 2>&1
    $dockerOK = $LASTEXITCODE -eq 0
} catch {}

if (-not $dockerOK) {
    Write-Host "  Docker daemon not responding — checking Docker Desktop..." -ForegroundColor Yellow
    
    $ddProcess = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
    if ($ddProcess) {
        Write-Host "  Docker Desktop process is running. The elevated session may not see the user-mode daemon." -ForegroundColor Yellow
        Write-Host "  Attempting to proceed anyway (Docker often works despite this)..." -ForegroundColor Yellow
    }
    
    # Try starting the Docker Desktop service
    $dockerService = Get-Service -Name "Docker Desktop Service" -ErrorAction SilentlyContinue
    if ($dockerService -and $dockerService.Status -ne "Running") {
        try { Start-Service -Name "Docker Desktop Service" -ErrorAction Stop } catch {}
        Start-Sleep -Seconds 5
    }
    
    # Update WSL kernel if needed
    try { wsl --update 2>$null | Out-Null } catch {}
    
    # Don't wait — try to proceed. If Docker truly isn't working,
    # the next step (pulling images) will fail with a clear error.
    Write-Host "  Proceeding with installation..." -ForegroundColor Yellow
}

try {
    & $docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Success "Docker is running"
    } else {
        Write-Host "  Docker daemon not responding in this session — will try anyway..." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Docker CLI not accessible — will try anyway..." -ForegroundColor Yellow
}

# ═══════════════════════════════════════════
# STEP 9: Pull AppVault Agent and start
# ═══════════════════════════════════════════
Step "Starting AppVault Agent"
Write-Host "  Pulling AppVault images..."

# Windows PowerShell 5.1 turns redirected native stderr into a terminating
# error when $ErrorActionPreference="Stop", and docker writes to stderr
# whenever the daemon isn't up yet. Relax EAP around the docker calls and
# check $LASTEXITCODE explicitly instead.
if (-not (Test-Path $docker) -and -not (Get-Command $docker -ErrorAction SilentlyContinue)) {
    Fail "Docker CLI not found ($docker) — install Docker Desktop and rerun this installer."
}
$dockerEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"

& $docker pull ghcr.io/sectutor/appvault-agent:latest 2>$null
$pullExit = $LASTEXITCODE
& $docker pull ghcr.io/sectutor/appvault-releases:v69 2>$null
if ($pullExit -eq 0) { $pullExit = $LASTEXITCODE }
if ($pullExit -ne 0) {
    $ErrorActionPreference = $dockerEAP
    Fail "Could not pull the AppVault images (docker exit code $pullExit). Make sure Docker Desktop is running, then run this installer again."
}

# Create data directory
mkdir "$env:USERPROFILE\.appvault\data" -Force | Out-Null
mkdir "$env:USERPROFILE\.appvault\apps" -Force | Out-Null

# Stop/remove only if the container already exists (avoids scary errors on first install)
if (& $docker ps -a --filter "name=^/appvault-agent$" --format '{{.Names}}' 2>$null | Select-String -Quiet "appvault-agent") {
    & $docker stop appvault-agent 2>$null | Out-Null
    & $docker rm appvault-agent 2>$null | Out-Null
}

# Wipe stale agent identity so it re-registers with current central
$stateFile = "$env:USERPROFILE\.appvault\data\agent_state.json"
if (Test-Path $stateFile) {
    Remove-Item $stateFile -Force
    Success "Cleared stale agent identity — will re-register on start"
}

# Docker socket mount - the Linux VM path works on Linux, macOS, AND Windows
# Docker Desktop (the engine runs in a Linux utility VM; the Windows named pipe
# cannot be bind-mounted into a Linux container, which left installs failing
# with "Docker unavailable" despite Docker being detected on the host).
$sockMount = "/var/run/docker.sock:/var/run/docker.sock"

# Start agent (Unauthenticated for local desktop — zero API key friction)
Write-Host "  Starting AppVault Agent on port 8086..."
& $docker run -d `
  --name appvault-agent `
  --restart unless-stopped `
  -p 8086:8086 `
  -v "$sockMount" `
  -v "$env:USERPROFILE\.appvault\data:/data" `
  -v "$env:USERPROFILE\.appvault\apps:/data/apps" `
  -e AGENT_PORT=8086 `
  -e CENTRAL_URL=https://appvault.airepoindex.com `
  -e AGENT_NAME="$env:COMPUTERNAME-agent" `
  -e STORAGE_PATH=/data `
  ghcr.io/sectutor/appvault-agent:latest
$agentRunExit = $LASTEXITCODE

Write-Host "  Starting App Store on port 8085..."
if (& $docker ps -a --filter "name=^/appvault-heimdall$" --format '{{.Names}}' 2>$null | Select-String -Quiet "appvault-heimdall") {
    & $docker stop appvault-heimdall 2>$null | Out-Null
    & $docker rm appvault-heimdall 2>$null | Out-Null
}

# Clear stale UI files — the store image re-seeds /config/www on first boot
Remove-Item "$env:USERPROFILE\.appvault\heimdall-config\www" -Recurse -Force -ErrorAction SilentlyContinue

& $docker run -d `
  --name appvault-heimdall `
  --restart unless-stopped `
  -p 8085:80 `
  -v "$env:USERPROFILE\.appvault\heimdall-config:/config" `
  -e CENTRAL_URL=https://appvault.airepoindex.com `
  -e PUID=1000 `
  -e PGID=1000 `
  -e TZ=Etc/UTC `
  ghcr.io/sectutor/appvault-releases:v69
$storeRunExit = $LASTEXITCODE

$ErrorActionPreference = $dockerEAP

if ($agentRunExit -ne 0) { Fail "Could not start the AppVault Agent container (docker exit code $agentRunExit)." }
if ($storeRunExit -ne 0) { Fail "Could not start the App Store container (docker exit code $storeRunExit)." }

# Register auto-start scheduled task so containers launch on Windows boot
Step "Configuring Windows Startup Task"
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command ""docker start appvault-agent appvault-heimdall"""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName "AppVaultAutoStart" -Action $action -Trigger $trigger -Description "Auto-start AppVault containers on login" -User $env:USERNAME -ErrorAction SilentlyContinue | Out-Null
    Success "Windows startup task 'AppVaultAutoStart' registered"
} catch {
    Warn "Could not register startup task automatically (non-critical)"
}





# ---- APPVAULT DESKTOP HELPER (embedded) ----
# Desktop Apps tab launcher: local HTTP helper that discovers Start-Menu apps,
# serves icons, and launches them. Installed to %USERPROFILE%\.appvault.
# (base64-embedded — the helper contains its own '@ here-strings)
Step "Installing Desktop App Helper (desktop launcher)"
try {
    $helperDir = "$env:USERPROFILE\.appvault"
    New-Item -ItemType Directory -Path $helperDir -Force | Out-Null
    $helperPath = Join-Path $helperDir "appvault-desktop.ps1"
    $helperContent = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PCMKLkFwcFZhdWx0IERlc2t0b3AgQXBwIEhlbHBlcgpMYXVuY2hlcyBkZXNrdG9wIGFwcGxpY2F0aW9ucyBmcm9tIHRoZSBBcHBWYXVsdCBsYXVuY2hlciAoRGVza3RvcCBBcHBzIHRhYikuCgpNb2RlczoKICBzZXJ2ZSAgICAgIChkZWZhdWx0KSBydW4gdGhlIGxvY2FsIEhUVFAgc2VydmVyIG9uIGh0dHA6Ly9sb2NhbGhvc3Q6ODc5MQogIHN0YXJ0ICAgICAgc3Bhd24gdGhlIHNlcnZlciBoaWRkZW4gYW5kIGV4aXQgKHVzZWQgYnkgdGhlIGFwcHZhdWx0Oi8vIHByb3RvY29sKQogIGxpc3QgICAgICAgcHJpbnQgdGhlIGRlc2t0b3AtYXBwIHJlZ2lzdHJ5IGFzIEpTT04KICBkaXNjb3ZlciAgIHByaW50IGluc3RhbGxlZCBhcHBzIGZvdW5kIGluIHRoZSBTdGFydCBNZW51IGFzIEpTT04KICBhZGQgICAgICAgIC1OYW1lIDxuPiAtUGF0aCA8cD4gIGFkZCBhbiBhcHAgdG8gdGhlIHJlZ2lzdHJ5CiAgcmVtb3ZlICAgICAtSWQgPGlkPiAgcmVtb3ZlIGFuIGFwcAogIGxhdW5jaCAgICAgLUlkIDxpZD4gIGxhdW5jaCBhbiBhcHAKClJlZ2lzdHJ5OiAlVVNFUlBST0ZJTEUlXC5hcHB2YXVsdFxkZXNrdG9wLWFwcHMuanNvbiAocGVyLW1hY2hpbmUgb24gcHVycG9zZSDigJQKZGVza3RvcCBhcHBzIGV4aXN0IG9ubHkgb24gdGhlIG1hY2hpbmUgdGhleSB3ZXJlIGluc3RhbGxlZCBvbikuCgpIVFRQIEFQSSAoQ09SUy1lbmFibGVkIGZvciB0aGUgQXBwVmF1bHQgc3RvcmUgcGFnZSk6CiAgR0VUICAvaGVhbHRoICAgICAgICAgICAgICAgICB7b2t9CiAgR0VUICAvYXBwcyAgICAgICAgICAgICAgICAgICByZWdpc3RyeQogIEdFVCAgL2Rpc2NvdmVyICAgICAgICAgICAgICAgU3RhcnQtTWVudSBhcHBzCiAgR0VUICAvaWNvbj9wPTxwYXRoPiAgICAgICAgICBQTkcgaWNvbiBmb3IgYW4gZXhlL2xuawogIFBPU1QgL2FkZCAgICB7bmFtZSxwYXRofQogIFBPU1QgL3JlbW92ZSB7aWR9CiAgUE9TVCAvbGF1bmNoIHtpZH0KIz4KcGFyYW0oCiAgICBbc3RyaW5nXSRNb2RlID0gInNlcnZlIiwKICAgIFtzdHJpbmddJE5hbWUgPSAiIiwKICAgIFtzdHJpbmddJFBhdGggPSAiIiwKICAgIFtzdHJpbmddJElkID0gIiIKKQoKJEVycm9yQWN0aW9uUHJlZmVyZW5jZSA9ICJTdG9wIgokUG9ydCA9IDg3OTEKJEJhc2UgPSAiaHR0cDovL2xvY2FsaG9zdDokUG9ydC8iCiRIZWxwZXJEaXIgPSBKb2luLVBhdGggJGVudjpVU0VSUFJPRklMRSAiLmFwcHZhdWx0IgokUmVnaXN0cnlQYXRoID0gSm9pbi1QYXRoICRIZWxwZXJEaXIgImRlc2t0b3AtYXBwcy5qc29uIgokU2NyaXB0UGF0aCA9ICRNeUludm9jYXRpb24uTXlDb21tYW5kLlBhdGgKQWRkLVR5cGUgLUFzc2VtYmx5TmFtZSBTeXN0ZW0uRHJhd2luZwoKZnVuY3Rpb24gR2V0LVJlZ2lzdHJ5IHsKICAgIGlmIChUZXN0LVBhdGggJFJlZ2lzdHJ5UGF0aCkgewogICAgICAgIHRyeSB7CiAgICAgICAgICAgICRyYXcgPSBHZXQtQ29udGVudCAkUmVnaXN0cnlQYXRoIC1SYXcgLUVycm9yQWN0aW9uIFN0b3AKICAgICAgICAgICAgaWYgKCRyYXcgLWFuZCAkcmF3LlRyaW0oKSkgeyByZXR1cm4gKCRyYXcgfCBDb252ZXJ0RnJvbS1Kc29uKSB9CiAgICAgICAgfSBjYXRjaCB7IH0KICAgIH0KICAgIHJldHVybiBAKCkKfQoKZnVuY3Rpb24gU2F2ZS1SZWdpc3RyeShbYXJyYXldJEFwcHMpIHsKICAgIGlmICgtbm90IChUZXN0LVBhdGggJEhlbHBlckRpcikpIHsgTmV3LUl0ZW0gLUl0ZW1UeXBlIERpcmVjdG9yeSAtUGF0aCAkSGVscGVyRGlyIC1Gb3JjZSB8IE91dC1OdWxsIH0KICAgICRqc29uID0gJEFwcHMgfCBDb252ZXJ0VG8tSnNvbiAtRGVwdGggNgogICAgW1N5c3RlbS5JTy5GaWxlXTo6V3JpdGVBbGxUZXh0KCRSZWdpc3RyeVBhdGgsICRqc29uLCBbU3lzdGVtLlRleHQuRW5jb2RpbmddOjpVVEY4KQp9CgpmdW5jdGlvbiBSZXNvbHZlLVRhcmdldChbc3RyaW5nXSRwKSB7CiAgICAjIG5vcm1hbGl6ZSBzZXBhcmF0b3JzIChmb3J3YXJkIHNsYXNoZXMgd29yayBldmVyeXdoZXJlIG9uIFdpbmRvd3MpCiAgICAkcCA9ICRwIC1yZXBsYWNlICJcXCIsICIvIgogICAgIyAubG5rIC0+IHJlc29sdmVkIGV4ZSBwYXRoOyBvdGhlcndpc2UgcmV0dXJuIGFzLWlzCiAgICB0cnkgewogICAgICAgIGlmICgkcCAtbWF0Y2ggIlwubG5rJCIpIHsKICAgICAgICAgICAgJHNoZWxsID0gTmV3LU9iamVjdCAtQ29tT2JqZWN0IFdTY3JpcHQuU2hlbGwKICAgICAgICAgICAgJHNjID0gJHNoZWxsLkNyZWF0ZVNob3J0Y3V0KCRwKQogICAgICAgICAgICBpZiAoJHNjLlRhcmdldFBhdGggLWFuZCAoVGVzdC1QYXRoICRzYy5UYXJnZXRQYXRoKSkgeyByZXR1cm4gJHNjLlRhcmdldFBhdGggfQogICAgICAgIH0KICAgIH0gY2F0Y2ggeyB9CiAgICByZXR1cm4gJHAKfQoKZnVuY3Rpb24gR2V0LURpc2NvdmVyZWQgewogICAgJGRpcnMgPSBAKCkKICAgICRwZCA9ICIkZW52OlByb2dyYW1EYXRhXE1pY3Jvc29mdFxXaW5kb3dzXFN0YXJ0IE1lbnVcUHJvZ3JhbXMiCiAgICAkYWQgPSAiJGVudjpBUFBEQVRBXE1pY3Jvc29mdFxXaW5kb3dzXFN0YXJ0IE1lbnVcUHJvZ3JhbXMiCiAgICBpZiAoVGVzdC1QYXRoICRwZCkgeyAkZGlycyArPSAkcGQgfQogICAgaWYgKFRlc3QtUGF0aCAkYWQpIHsgJGRpcnMgKz0gJGFkIH0KICAgICRzaGVsbCA9IE5ldy1PYmplY3QgLUNvbU9iamVjdCBXU2NyaXB0LlNoZWxsCiAgICAkc2VlbiA9IEB7fQogICAgJG91dCA9IEAoKQogICAgZm9yZWFjaCAoJGQgaW4gJGRpcnMpIHsKICAgICAgICBHZXQtQ2hpbGRJdGVtIC1QYXRoICRkIC1SZWN1cnNlIC1GaWx0ZXIgIioubG5rIiAtRXJyb3JBY3Rpb24gU2lsZW50bHlDb250aW51ZSB8IEZvckVhY2gtT2JqZWN0IHsKICAgICAgICAgICAgdHJ5IHsKICAgICAgICAgICAgICAgICRzYyA9ICRzaGVsbC5DcmVhdGVTaG9ydGN1dCgkXy5GdWxsTmFtZSkKICAgICAgICAgICAgICAgICR0YXJnZXQgPSAkc2MuVGFyZ2V0UGF0aAogICAgICAgICAgICAgICAgaWYgKC1ub3QgJHRhcmdldCkgeyByZXR1cm4gfQogICAgICAgICAgICAgICAgaWYgKC1ub3QgKFRlc3QtUGF0aCAkdGFyZ2V0KSkgeyByZXR1cm4gfQogICAgICAgICAgICAgICAgJGtleSA9ICR0YXJnZXQuVG9Mb3dlcigpCiAgICAgICAgICAgICAgICBpZiAoJHNlZW4uQ29udGFpbnNLZXkoJGtleSkpIHsgcmV0dXJuIH0KICAgICAgICAgICAgICAgICRzZWVuWyRrZXldID0gJHRydWUKICAgICAgICAgICAgICAgICRvdXQgKz0gW1BTQ3VzdG9tT2JqZWN0XUB7CiAgICAgICAgICAgICAgICAgICAgaWQgICAgID0gW2d1aWRdOjpOZXdHdWlkKCkuVG9TdHJpbmcoIk4iKQogICAgICAgICAgICAgICAgICAgIG5hbWUgICA9ICRfLkJhc2VOYW1lCiAgICAgICAgICAgICAgICAgICAgcGF0aCAgID0gKCRfLkZ1bGxOYW1lIC1yZXBsYWNlICJcXCIsICIvIikKICAgICAgICAgICAgICAgICAgICB0YXJnZXQgPSAoJHRhcmdldCAtcmVwbGFjZSAiXFwiLCAiLyIpCiAgICAgICAgICAgICAgICB9CiAgICAgICAgICAgIH0gY2F0Y2ggeyB9CiAgICAgICAgfQogICAgfQogICAgcmV0dXJuICgkb3V0IHwgU29ydC1PYmplY3QgbmFtZSkKfQoKZnVuY3Rpb24gR2V0LUljb25CeXRlcyhbc3RyaW5nXSRwKSB7CiAgICBpZiAoLW5vdCAkcCAtb3IgLW5vdCAoVGVzdC1QYXRoICRwKSkgeyByZXR1cm4gJG51bGwgfQogICAgIyBTSEdldEZpbGVJbmZvIHJlc29sdmVzIC5sbmsgaWNvbnMgQ09SUkVDVExZIChFeHRyYWN0QXNzb2NpYXRlZEljb24gcmV0dXJucwogICAgIyBhIGdlbmVyaWMgMjUxLWJ5dGUgaWNvbiBmb3Igc2hvcnRjdXRzKSBhbmQgZ2l2ZXMgdGhlIHNoZWxsLXNpemUgaWNvbi4KICAgICMgTk9URTogdGhlIFdpbjMyIEFQSSBSRUpFQ1RTIGZvcndhcmQgc2xhc2hlcyDigJQgcGFzcyBhIG5hdGl2ZSBiYWNrc2xhc2ggcGF0aC4KICAgICRpY29uID0gJG51bGw7ICRibXAgPSAkbnVsbDsgJG1zID0gJG51bGwKICAgIHRyeSB7CiAgICAgICAgaWYgKC1ub3QgJHNjcmlwdDpTaGVsbFR5cGUpIHsKICAgICAgICAgICAgJHNjcmlwdDpTaGVsbFR5cGUgPSBBZGQtVHlwZSAtTWVtYmVyRGVmaW5pdGlvbiBAJwpbRGxsSW1wb3J0KCJzaGVsbDMyLmRsbCIsIENoYXJTZXQgPSBDaGFyU2V0LlVuaWNvZGUpXQpwdWJsaWMgc3RhdGljIGV4dGVybiBJbnRQdHIgU0hHZXRGaWxlSW5mbyhzdHJpbmcgcHN6UGF0aCwgdWludCBkd0ZpbGVBdHRyaWJ1dGVzLCBvdXQgU0hGSUxFSU5GTyBwc2ZpLCB1aW50IGNiU2l6ZUZpbGVJbmZvLCB1aW50IHVGbGFncyk7CltEbGxJbXBvcnQoInVzZXIzMi5kbGwiKV0KcHVibGljIHN0YXRpYyBleHRlcm4gYm9vbCBEZXN0cm95SWNvbihJbnRQdHIgaEljb24pOwpbU3RydWN0TGF5b3V0KExheW91dEtpbmQuU2VxdWVudGlhbCwgQ2hhclNldCA9IENoYXJTZXQuVW5pY29kZSldCnB1YmxpYyBzdHJ1Y3QgU0hGSUxFSU5GTyB7CiAgICBwdWJsaWMgSW50UHRyIGhJY29uOwogICAgcHVibGljIGludCBpSWNvbjsKICAgIHB1YmxpYyB1aW50IGR3QXR0cmlidXRlczsKICAgIFtNYXJzaGFsQXMoVW5tYW5hZ2VkVHlwZS5CeVZhbFRTdHIsIFNpemVDb25zdCA9IDI2MCldCiAgICBwdWJsaWMgc3RyaW5nIHN6RGlzcGxheU5hbWU7CiAgICBbTWFyc2hhbEFzKFVubWFuYWdlZFR5cGUuQnlWYWxUU3RyLCBTaXplQ29uc3QgPSA4MCldCiAgICBwdWJsaWMgc3RyaW5nIHN6VHlwZU5hbWU7Cn0KJ0AgLU5hbWUgU2hlbGwgLU5hbWVzcGFjZSBXaW4zMiAtUGFzc1RocnUKICAgICAgICB9CiAgICAgICAgJG5hdGl2ZSA9ICRwIC1yZXBsYWNlICIvIiwgIlwiCiAgICAgICAgIyBTSEdGSV9JQ09OICgweDEwMCkgfCBTSEdGSV9TSEVMTElDT05TSVpFICgweDQpID0gc2hlbGwtc2l6ZSByZWFsIGljb24KICAgICAgICAkaW5mbyA9IE5ldy1PYmplY3QgV2luMzIuU2hlbGwrU0hGSUxFSU5GTwogICAgICAgICRyZXMgPSBbV2luMzIuU2hlbGxdOjpTSEdldEZpbGVJbmZvKCRuYXRpdmUsIDAsIFtyZWZdJGluZm8sCiAgICAgICAgICAgICAgIFtTeXN0ZW0uUnVudGltZS5JbnRlcm9wU2VydmljZXMuTWFyc2hhbF06OlNpemVPZigkaW5mbyksIDB4MTA0KQogICAgICAgIGlmICgkcmVzIC1lcSBbSW50UHRyXTo6WmVybyAtb3IgJGluZm8uaEljb24gLWVxIFtJbnRQdHJdOjpaZXJvKSB7CiAgICAgICAgICAgICMgZmFsbGJhY2s6IHBsYWluIGFzc29jaWF0ZWQtaWNvbiBleHRyYWN0aW9uCiAgICAgICAgICAgICRpY29uID0gW1N5c3RlbS5EcmF3aW5nLkljb25dOjpFeHRyYWN0QXNzb2NpYXRlZEljb24oJHApCiAgICAgICAgICAgIGlmICgtbm90ICRpY29uKSB7IHJldHVybiAkbnVsbCB9CiAgICAgICAgICAgICRibXAgPSAkaWNvbi5Ub0JpdG1hcCgpCiAgICAgICAgfSBlbHNlIHsKICAgICAgICAgICAgJGljb24gPSBbU3lzdGVtLkRyYXdpbmcuSWNvbl06OkZyb21IYW5kbGUoJGluZm8uaEljb24pCiAgICAgICAgICAgICRibXAgPSAkaWNvbi5Ub0JpdG1hcCgpCiAgICAgICAgICAgIFtXaW4zMi5TaGVsbF06OkRlc3Ryb3lJY29uKCRpbmZvLmhJY29uKSB8IE91dC1OdWxsCiAgICAgICAgfQogICAgICAgICRtcyA9IE5ldy1PYmplY3QgU3lzdGVtLklPLk1lbW9yeVN0cmVhbQogICAgICAgICRibXAuU2F2ZSgkbXMsIFtTeXN0ZW0uRHJhd2luZy5JbWFnaW5nLkltYWdlRm9ybWF0XTo6UG5nKQogICAgICAgICMgcGxhaW4gcmV0dXJuIChieXRlW10gdW5yb2xscyB0byBieXRlcyBvbiB0aGUgcGlwZWxpbmUpIOKAlCBjYWxsZXIKICAgICAgICAjIGNvbGxlY3RzIHdpdGggQCgpIGFuZCBjYXN0cyBbYnl0ZVtdXSwgd2hpY2ggaXMgdGhlIHJlbGlhYmxlIHBhdHRlcm4KICAgICAgICByZXR1cm4gJG1zLlRvQXJyYXkoKQogICAgfSBjYXRjaCB7CiAgICAgICAgcmV0dXJuICRudWxsCiAgICB9IGZpbmFsbHkgewogICAgICAgIGlmICgkbXMpIHsgJG1zLkRpc3Bvc2UoKSB9CiAgICAgICAgaWYgKCRibXApIHsgJGJtcC5EaXNwb3NlKCkgfQogICAgfQp9CgpmdW5jdGlvbiBTZW5kLUpzb24oJGN0eCwgW2ludF0kY29kZSwgJG9iaikgewogICAgJGJvZHkgPSAoJG9iaiB8IENvbnZlcnRUby1Kc29uIC1EZXB0aCA4KQogICAgJGJ5dGVzID0gW1N5c3RlbS5UZXh0LkVuY29kaW5nXTo6VVRGOC5HZXRCeXRlcygkYm9keSkKICAgICRyZXNwID0gJGN0eC5SZXNwb25zZQogICAgJHJlc3AuU3RhdHVzQ29kZSA9ICRjb2RlCiAgICAkcmVzcC5Db250ZW50VHlwZSA9ICJhcHBsaWNhdGlvbi9qc29uOyBjaGFyc2V0PXV0Zi04IgogICAgJHJlc3AuSGVhZGVycy5BZGQoIkFjY2Vzcy1Db250cm9sLUFsbG93LU9yaWdpbiIsICIqIikKICAgICRyZXNwLkhlYWRlcnMuQWRkKCJBY2Nlc3MtQ29udHJvbC1BbGxvdy1NZXRob2RzIiwgIkdFVCwgUE9TVCwgT1BUSU9OUyIpCiAgICAkcmVzcC5IZWFkZXJzLkFkZCgiQWNjZXNzLUNvbnRyb2wtQWxsb3ctSGVhZGVycyIsICJDb250ZW50LVR5cGUiKQogICAgJHJlc3AuQ29udGVudExlbmd0aDY0ID0gJGJ5dGVzLkxlbmd0aAogICAgJHJlc3AuT3V0cHV0U3RyZWFtLldyaXRlKCRieXRlcywgMCwgJGJ5dGVzLkxlbmd0aCkKICAgICRyZXNwLk91dHB1dFN0cmVhbS5DbG9zZSgpCn0KCmZ1bmN0aW9uIFNlbmQtRW1wdHkoJGN0eCwgW2ludF0kY29kZSkgewogICAgJHJlc3AgPSAkY3R4LlJlc3BvbnNlCiAgICAkcmVzcC5TdGF0dXNDb2RlID0gJGNvZGUKICAgICRyZXNwLkhlYWRlcnMuQWRkKCJBY2Nlc3MtQ29udHJvbC1BbGxvdy1PcmlnaW4iLCAiKiIpCiAgICAkcmVzcC5IZWFkZXJzLkFkZCgiQWNjZXNzLUNvbnRyb2wtQWxsb3ctTWV0aG9kcyIsICJHRVQsIFBPU1QsIE9QVElPTlMiKQogICAgJHJlc3AuSGVhZGVycy5BZGQoIkFjY2Vzcy1Db250cm9sLUFsbG93LUhlYWRlcnMiLCAiQ29udGVudC1UeXBlIikKICAgICRyZXNwLkNvbnRlbnRMZW5ndGg2NCA9IDAKICAgICRyZXNwLk91dHB1dFN0cmVhbS5DbG9zZSgpCn0KCmZ1bmN0aW9uIEhhbmRsZS1SZXF1ZXN0KCRjdHgpIHsKICAgICRyZXEgPSAkY3R4LlJlcXVlc3QKICAgICRtZXRob2QgPSAkcmVxLkh0dHBNZXRob2QKICAgICRwYXRoID0gJHJlcS5VcmwuQWJzb2x1dGVQYXRoCgogICAgaWYgKCRtZXRob2QgLWVxICJPUFRJT05TIikgeyBTZW5kLUVtcHR5ICRjdHggMjA0OyByZXR1cm4gfQogICAgaWYgKCRwYXRoIC1lcSAiL2hlYWx0aCIpIHsgU2VuZC1Kc29uICRjdHggMjAwIEB7IG9rID0gJHRydWU7IHBpZCA9ICRQSUQgfTsgcmV0dXJuIH0KICAgIGlmICgkcGF0aCAtZXEgIi9hcHBzIikgewogICAgICAgIFNlbmQtSnNvbiAkY3R4IDIwMCBAeyBvayA9ICR0cnVlOyBhcHBzID0gQChHZXQtUmVnaXN0cnkpIH0KICAgICAgICByZXR1cm4KICAgIH0KICAgIGlmICgkcGF0aCAtZXEgIi9kaXNjb3ZlciIpIHsKICAgICAgICBTZW5kLUpzb24gJGN0eCAyMDAgQHsgb2sgPSAkdHJ1ZTsgYXBwcyA9IEAoR2V0LURpc2NvdmVyZWQpIH0KICAgICAgICByZXR1cm4KICAgIH0KICAgIGlmICgkcGF0aCAtZXEgIi9pY29uIiAtYW5kICRtZXRob2QgLWVxICJHRVQiKSB7CiAgICAgICAgJGlkID0gJHJlcS5RdWVyeVN0cmluZ1siaWQiXQogICAgICAgICRhcHAgPSBAKEdldC1SZWdpc3RyeSkgfCBXaGVyZS1PYmplY3QgeyAkXy5pZCAtZXEgJGlkIH0gfCBTZWxlY3QtT2JqZWN0IC1GaXJzdCAxCiAgICAgICAgaWYgKC1ub3QgJGFwcCkgeyBTZW5kLUVtcHR5ICRjdHggMjA0OyByZXR1cm4gfQogICAgICAgIFtieXRlW11dJGJ5dGVzID0gQChHZXQtSWNvbkJ5dGVzICIkKCRhcHAucGF0aCkiKQogICAgICAgIGlmICgtbm90ICRieXRlcyAtb3IgJGJ5dGVzLkxlbmd0aCAtZXEgMCkgeyBTZW5kLUVtcHR5ICRjdHggMjA0OyByZXR1cm4gfQogICAgICAgICRyZXNwID0gJGN0eC5SZXNwb25zZQogICAgICAgICRyZXNwLlN0YXR1c0NvZGUgPSAyMDAKICAgICAgICAkcmVzcC5Db250ZW50VHlwZSA9ICJpbWFnZS9wbmciCiAgICAgICAgJHJlc3AuSGVhZGVycy5BZGQoIkFjY2Vzcy1Db250cm9sLUFsbG93LU9yaWdpbiIsICIqIikKICAgICAgICAkcmVzcC5Db250ZW50TGVuZ3RoNjQgPSAkYnl0ZXMuTGVuZ3RoCiAgICAgICAgJHJlc3AuT3V0cHV0U3RyZWFtLldyaXRlKCRieXRlcywgMCwgJGJ5dGVzLkxlbmd0aCkKICAgICAgICAkcmVzcC5PdXRwdXRTdHJlYW0uQ2xvc2UoKQogICAgICAgIHJldHVybgogICAgfQogICAgaWYgKCRtZXRob2QgLWVxICJQT1NUIiAtYW5kICgkcGF0aCAtZXEgIi9hZGQiIC1vciAkcGF0aCAtZXEgIi9yZW1vdmUiIC1vciAkcGF0aCAtZXEgIi9sYXVuY2giKSkgewogICAgICAgICRyZWFkZXIgPSBOZXctT2JqZWN0IFN5c3RlbS5JTy5TdHJlYW1SZWFkZXIoJHJlcS5JbnB1dFN0cmVhbSwgW1N5c3RlbS5UZXh0LkVuY29kaW5nXTo6VVRGOCkKICAgICAgICAkYm9keSA9ICRyZWFkZXIuUmVhZFRvRW5kKCkKICAgICAgICAkcmVhZGVyLkNsb3NlKCkKICAgICAgICB0cnkgeyAkZGF0YSA9ICRib2R5IHwgQ29udmVydEZyb20tSnNvbiB9IGNhdGNoIHsgU2VuZC1Kc29uICRjdHggNDAwIEB7IG9rID0gJGZhbHNlOyBlcnJvciA9ICJiYWQganNvbiIgfTsgcmV0dXJuIH0KCiAgICAgICAgaWYgKCRwYXRoIC1lcSAiL2FkZCIpIHsKICAgICAgICAgICAgJG5hbWUgPSAiJCgkZGF0YS5uYW1lKSIuVHJpbSgpCiAgICAgICAgICAgICRwID0gKCIkKCRkYXRhLnBhdGgpIi5UcmltKCkgLXJlcGxhY2UgIlxcIiwgIi8iKQogICAgICAgICAgICBpZiAoLW5vdCAkbmFtZSAtb3IgLW5vdCAkcCkgeyBTZW5kLUpzb24gJGN0eCA0MDAgQHsgb2sgPSAkZmFsc2U7IGVycm9yID0gIm5hbWUrcGF0aCByZXF1aXJlZCIgfTsgcmV0dXJuIH0KICAgICAgICAgICAgJHJlZyA9IEAoR2V0LVJlZ2lzdHJ5KQogICAgICAgICAgICAkZXhpc3RpbmcgPSAkcmVnIHwgV2hlcmUtT2JqZWN0IHsgJF8ucGF0aCAtaWVxICRwIH0KICAgICAgICAgICAgaWYgKCRleGlzdGluZykgeyBTZW5kLUpzb24gJGN0eCAyMDAgQHsgb2sgPSAkdHJ1ZTsgYXBwID0gJGV4aXN0aW5nIH07IHJldHVybiB9CiAgICAgICAgICAgICRhcHAgPSBbUFNDdXN0b21PYmplY3RdQHsKICAgICAgICAgICAgICAgIGlkICAgICA9IFtndWlkXTo6TmV3R3VpZCgpLlRvU3RyaW5nKCJOIikKICAgICAgICAgICAgICAgIG5hbWUgICA9ICRuYW1lCiAgICAgICAgICAgICAgICBwYXRoICAgPSAkcAogICAgICAgICAgICAgICAgdGFyZ2V0ID0gKFJlc29sdmUtVGFyZ2V0ICRwKQogICAgICAgICAgICAgICAgYWRkZWQgID0gKEdldC1EYXRlKS5Ub1N0cmluZygieXl5eS1NTS1kZCBISDptbSIpCiAgICAgICAgICAgIH0KICAgICAgICAgICAgJHJlZyArPSAkYXBwCiAgICAgICAgICAgIFNhdmUtUmVnaXN0cnkgJHJlZwogICAgICAgICAgICBTZW5kLUpzb24gJGN0eCAyMDAgQHsgb2sgPSAkdHJ1ZTsgYXBwID0gJGFwcCB9CiAgICAgICAgICAgIHJldHVybgogICAgICAgIH0KICAgICAgICBpZiAoJHBhdGggLWVxICIvcmVtb3ZlIikgewogICAgICAgICAgICAkaWQgPSAiJCgkZGF0YS5pZCkiLlRyaW0oKQogICAgICAgICAgICAkcmVnID0gQChHZXQtUmVnaXN0cnkpIHwgV2hlcmUtT2JqZWN0IHsgJF8uaWQgLW5lICRpZCB9CiAgICAgICAgICAgIFNhdmUtUmVnaXN0cnkgJHJlZwogICAgICAgICAgICBTZW5kLUpzb24gJGN0eCAyMDAgQHsgb2sgPSAkdHJ1ZSB9CiAgICAgICAgICAgIHJldHVybgogICAgICAgIH0KICAgICAgICBpZiAoJHBhdGggLWVxICIvbGF1bmNoIikgewogICAgICAgICAgICAkaWQgPSAiJCgkZGF0YS5pZCkiLlRyaW0oKQogICAgICAgICAgICAkYXBwID0gQChHZXQtUmVnaXN0cnkpIHwgV2hlcmUtT2JqZWN0IHsgJF8uaWQgLWVxICRpZCB9IHwgU2VsZWN0LU9iamVjdCAtRmlyc3QgMQogICAgICAgICAgICBpZiAoLW5vdCAkYXBwKSB7IFNlbmQtSnNvbiAkY3R4IDQwNCBAeyBvayA9ICRmYWxzZTsgZXJyb3IgPSAiYXBwIG5vdCBmb3VuZCIgfTsgcmV0dXJuIH0KICAgICAgICAgICAgJHRhcmdldCA9ICRhcHAucGF0aAogICAgICAgICAgICBpZiAoLW5vdCAoVGVzdC1QYXRoICR0YXJnZXQpKSB7IFNlbmQtSnNvbiAkY3R4IDQwNCBAeyBvayA9ICRmYWxzZTsgZXJyb3IgPSAiYXBwIG5vdCBmb3VuZCBvbiBkaXNrOiAkdGFyZ2V0IiB9OyByZXR1cm4gfQogICAgICAgICAgICAkcCA9IFN0YXJ0LVByb2Nlc3MgLUZpbGVQYXRoICR0YXJnZXQgLVBhc3NUaHJ1CiAgICAgICAgICAgICMgbGF1bmNoZWQgZnJvbSBhIGJhY2tncm91bmQgaGVscGVyOiB0aGUgd2luZG93IG9wZW5zIGJ1dCBpcyBOT1QKICAgICAgICAgICAgIyBhY3RpdmF0ZWQgKHNpdHMgbWluaW1pemVkL2JlaGluZCkg4oCUIEFwcEFjdGl2YXRlIGJyaW5ncyBpdCB1cAogICAgICAgICAgICB0cnkgewogICAgICAgICAgICAgICAgU3RhcnQtU2xlZXAgLU1pbGxpc2Vjb25kcyA3MDAKICAgICAgICAgICAgICAgICR3cyA9IE5ldy1PYmplY3QgLUNvbU9iamVjdCBXU2NyaXB0LlNoZWxsCiAgICAgICAgICAgICAgICAkbnVsbCA9ICR3cy5BcHBBY3RpdmF0ZSgkcC5JZCkKICAgICAgICAgICAgfSBjYXRjaCB7IH0KICAgICAgICAgICAgU2VuZC1Kc29uICRjdHggMjAwIEB7IG9rID0gJHRydWU7IGxhdW5jaGVkID0gJGFwcC5uYW1lOyBwaWQgPSAkcC5JZCB9CiAgICAgICAgICAgIHJldHVybgogICAgICAgIH0KICAgIH0KICAgIFNlbmQtSnNvbiAkY3R4IDQwNCBAeyBvayA9ICRmYWxzZTsgZXJyb3IgPSAibm90IGZvdW5kOiAkcGF0aCIgfQp9CgpmdW5jdGlvbiBTdGFydC1TZXJ2ZXIgewogICAgJGxpc3RlbmVyID0gTmV3LU9iamVjdCBTeXN0ZW0uTmV0Lkh0dHBMaXN0ZW5lcgogICAgIyBsb2NhbGhvc3QgcHJlZml4IChubyBVUkwgQUNMIG5lZWRlZCkuIENvbnRhaW5lcnMgcmVhY2ggaXQgdmlhCiAgICAjIGhvc3QuZG9ja2VyLmludGVybmFsIFdJVEggYSBIb3N0OiBsb2NhbGhvc3Q6ODc5MSBoZWFkZXIgKHRoZSBnYXRld2F5J3MKICAgICMgZGVza3RvcCB0b29scyBzZW5kIHRoYXQgb3ZlcnJpZGUpIOKAlCBIVFRQLnN5cyBtYXRjaGVzIGxvb3BiYWNrIEhvc3RzIGhlcmUuCiAgICAkbGlzdGVuZXIuUHJlZml4ZXMuQWRkKCJodHRwOi8vbG9jYWxob3N0Ojg3OTEvIikKICAgIHRyeSB7ICRsaXN0ZW5lci5TdGFydCgpIH0gY2F0Y2ggewogICAgICAgICMgcG9ydCBhbHJlYWR5IGluIHVzZSAtPiBhbm90aGVyIGhlbHBlciBpcyBydW5uaW5nCiAgICAgICAgV3JpdGUtSG9zdCAiaGVscGVyIGFscmVhZHkgcnVubmluZyIKICAgICAgICByZXR1cm4KICAgIH0KICAgIFdyaXRlLUhvc3QgIkFwcFZhdWx0IERlc2t0b3AgSGVscGVyIGxpc3RlbmluZyBvbiAkQmFzZSAocGlkICRQSUQpIgogICAgd2hpbGUgKCR0cnVlKSB7CiAgICAgICAgdHJ5IHsKICAgICAgICAgICAgJGN0eCA9ICRsaXN0ZW5lci5HZXRDb250ZXh0KCkKICAgICAgICAgICAgdHJ5IHsgSGFuZGxlLVJlcXVlc3QgJGN0eCB9IGNhdGNoIHsgdHJ5IHsgU2VuZC1Kc29uICRjdHggNTAwIEB7IG9rID0gJGZhbHNlOyBlcnJvciA9ICIkKCRfLkV4Y2VwdGlvbi5NZXNzYWdlKSIgfSB9IGNhdGNoIHsgfSB9CiAgICAgICAgfSBjYXRjaCB7CiAgICAgICAgICAgIFN0YXJ0LVNsZWVwIC1NaWxsaXNlY29uZHMgMjAwCiAgICAgICAgfQogICAgfQp9CgpmdW5jdGlvbiBTdGFydC1IaWRkZW4gewogICAgIyBzcGF3biB0aGUgc2VydmVyIGhpZGRlbiBhbmQgZXhpdCAocHJvdG9jb2wtaGFuZGxlciBlbnRyeSkKICAgICRydW5uaW5nID0gR2V0LU5ldFRDUENvbm5lY3Rpb24gLUxvY2FsUG9ydCAkUG9ydCAtU3RhdGUgTGlzdGVuIC1FcnJvckFjdGlvbiBTaWxlbnRseUNvbnRpbnVlCiAgICBpZiAoLW5vdCAkcnVubmluZykgewogICAgICAgIFN0YXJ0LVByb2Nlc3MgcG93ZXJzaGVsbC5leGUgLUFyZ3VtZW50TGlzdCBAKAogICAgICAgICAgICAiLU5vUHJvZmlsZSIsICItRXhlY3V0aW9uUG9saWN5IiwgIkJ5cGFzcyIsICItV2luZG93U3R5bGUiLCAiSGlkZGVuIiwKICAgICAgICAgICAgIi1GaWxlIiwgImAiJFNjcmlwdFBhdGhgIiIsICJzZXJ2ZSIKICAgICAgICApIC1XaW5kb3dTdHlsZSBIaWRkZW4KICAgIH0KfQoKIyDilIDilIAgQ0xJIG1vZGVzIOKUgOKUgApzd2l0Y2ggKCRNb2RlKSB7CiAgICAic2VydmUiICAgIHsgU3RhcnQtU2VydmVyOyBicmVhayB9CiAgICAic3RhcnQiICAgIHsgU3RhcnQtSGlkZGVuOyBicmVhayB9CiAgICAibGlzdCIgICAgIHsgQChHZXQtUmVnaXN0cnkpIHwgQ29udmVydFRvLUpzb24gLURlcHRoIDY7IGJyZWFrIH0KICAgICJkaXNjb3ZlciIgeyBAKEdldC1EaXNjb3ZlcmVkKSB8IENvbnZlcnRUby1Kc29uIC1EZXB0aCA2OyBicmVhayB9CiAgICAiYWRkIiAgICAgIHsKICAgICAgICAkcmVnID0gQChHZXQtUmVnaXN0cnkpCiAgICAgICAgJGFwcCA9IFtQU0N1c3RvbU9iamVjdF1AeyBpZCA9IFtndWlkXTo6TmV3R3VpZCgpLlRvU3RyaW5nKCJOIik7IG5hbWUgPSAkTmFtZTsgcGF0aCA9ICRQYXRoOyB0YXJnZXQgPSAoUmVzb2x2ZS1UYXJnZXQgJFBhdGgpOyBhZGRlZCA9IChHZXQtRGF0ZSkuVG9TdHJpbmcoInl5eXktTU0tZGQgSEg6bW0iKSB9CiAgICAgICAgJHJlZyArPSAkYXBwOyBTYXZlLVJlZ2lzdHJ5ICRyZWcKICAgICAgICAkYXBwIHwgQ29udmVydFRvLUpzb247IGJyZWFrCiAgICB9CiAgICAicmVtb3ZlIiAgIHsgU2F2ZS1SZWdpc3RyeSAoQChHZXQtUmVnaXN0cnkpIHwgV2hlcmUtT2JqZWN0IHsgJF8uaWQgLW5lICRJZCB9KTsgInJlbW92ZWQgJElkIjsgYnJlYWsgfQogICAgImxhdW5jaCIgICB7CiAgICAgICAgJGFwcCA9IEAoR2V0LVJlZ2lzdHJ5KSB8IFdoZXJlLU9iamVjdCB7ICRfLmlkIC1lcSAkSWQgfSB8IFNlbGVjdC1PYmplY3QgLUZpcnN0IDEKICAgICAgICBpZiAoJGFwcCkgeyBTdGFydC1Qcm9jZXNzIC1GaWxlUGF0aCAkYXBwLnBhdGg7ICJsYXVuY2hlZCAkKCRhcHAubmFtZSkiIH0gZWxzZSB7ICJub3QgZm91bmQiIH0KICAgICAgICBicmVhawogICAgfQogICAgZGVmYXVsdCAgICB7IFN0YXJ0LVNlcnZlciB9Cn0K'))
    [System.IO.File]::WriteAllText($helperPath, $helperContent, [System.Text.Encoding]::UTF8)
    # auto-start at login (hidden window)
    $da = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$helperPath`" serve"
    Register-ScheduledTask -TaskName "AppVaultDesktopHelper" -Action $da -Trigger (New-ScheduledTaskTrigger -AtLogOn) -User $env:USERNAME -Description "AppVault desktop app launcher helper" -ErrorAction SilentlyContinue | Out-Null
    Start-ScheduledTask -TaskName "AppVaultDesktopHelper" -ErrorAction SilentlyContinue
    # appvault:// protocol handler (start helper on demand from the store page)
    New-Item "HKCU:\Software\Classes\appvault\shell\open\command" -Force | Out-Null
    Set-ItemProperty "HKCU:\Software\Classes\appvault\shell\open\command" "(default)" "`"$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"& '$helperPath' start`""
    # URL ACL so the helper (user-session process) can bind http://+:8791/ —
    # required for containers reaching it via host.docker.internal
    netsh http add urlacl url=http://+:8791/ user=Everyone | Out-Null
    Success "Desktop helper installed — auto-starts at login"
} catch {
    Warn "Desktop helper setup failed (non-critical): $($_.Exception.Message)"
}

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
if ($Host.UI.RawUI -and $Host.Name -notlike "*NonInteractive*") {
    Write-Host "  Press any key to open the App Store..."
    try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch {}
}
try { Start-Process "http://localhost:8085/" } catch {}
