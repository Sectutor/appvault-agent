<#
.AppVault Desktop App Helper
Launches desktop applications from the AppVault launcher (Desktop Apps tab).

Modes:
  serve      (default) run the local HTTP server on http://localhost:8791
  start      spawn the server hidden and exit (used by the appvault:// protocol)
  list       print the desktop-app registry as JSON
  discover   print installed apps found in the Start Menu as JSON
  add        -Name <n> -Path <p>  add an app to the registry
  remove     -Id <id>  remove an app
  launch     -Id <id>  launch an app

Registry: %USERPROFILE%\.appvault\desktop-apps.json (per-machine on purpose —
desktop apps exist only on the machine they were installed on).

HTTP API (CORS-enabled for the AppVault store page):
  GET  /health                 {ok}
  GET  /apps                   registry
  GET  /discover               Start-Menu apps
  GET  /icon?p=<path>          PNG icon for an exe/lnk
  POST /add    {name,path}
  POST /remove {id}
  POST /launch {id}
#>
param(
    [string]$Mode = "serve",
    [string]$Name = "",
    [string]$Path = "",
    [string]$Id = ""
)

$ErrorActionPreference = "Stop"
$Port = 8791
$Base = "http://localhost:$Port/"
$HelperDir = Join-Path $env:USERPROFILE ".appvault"
$RegistryPath = Join-Path $HelperDir "desktop-apps.json"
$ScriptPath = $MyInvocation.MyCommand.Path
Add-Type -AssemblyName System.Drawing

function Get-Registry {
    if (Test-Path $RegistryPath) {
        try {
            $raw = Get-Content $RegistryPath -Raw -ErrorAction Stop
            if ($raw -and $raw.Trim()) { return ($raw | ConvertFrom-Json) }
        } catch { }
    }
    return @()
}

function Save-Registry([array]$Apps) {
    if (-not (Test-Path $HelperDir)) { New-Item -ItemType Directory -Path $HelperDir -Force | Out-Null }
    $json = $Apps | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($RegistryPath, $json, [System.Text.Encoding]::UTF8)
}

function Resolve-Target([string]$p) {
    # normalize separators (forward slashes work everywhere on Windows)
    $p = $p -replace "\\", "/"
    # .lnk -> resolved exe path; otherwise return as-is
    try {
        if ($p -match "\.lnk$") {
            $shell = New-Object -ComObject WScript.Shell
            $sc = $shell.CreateShortcut($p)
            if ($sc.TargetPath -and (Test-Path $sc.TargetPath)) { return $sc.TargetPath }
        }
    } catch { }
    return $p
}

function Get-Discovered {
    $dirs = @()
    $pd = "$env:ProgramData\Microsoft\Windows\Start Menu\Programs"
    $ad = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
    if (Test-Path $pd) { $dirs += $pd }
    if (Test-Path $ad) { $dirs += $ad }
    $shell = New-Object -ComObject WScript.Shell
    $seen = @{}
    $out = @()
    foreach ($d in $dirs) {
        Get-ChildItem -Path $d -Recurse -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                $sc = $shell.CreateShortcut($_.FullName)
                $target = $sc.TargetPath
                if (-not $target) { return }
                if (-not (Test-Path $target)) { return }
                $key = $target.ToLower()
                if ($seen.ContainsKey($key)) { return }
                $seen[$key] = $true
                $out += [PSCustomObject]@{
                    id     = [guid]::NewGuid().ToString("N")
                    name   = $_.BaseName
                    path   = ($_.FullName -replace "\\", "/")
                    target = ($target -replace "\\", "/")
                }
            } catch { }
        }
    }
    return ($out | Sort-Object name)
}

function Get-IconBytes([string]$p) {
    if (-not $p -or -not (Test-Path $p)) { return $null }
    # SHGetFileInfo resolves .lnk icons CORRECTLY (ExtractAssociatedIcon returns
    # a generic 251-byte icon for shortcuts) and gives the shell-size icon.
    # NOTE: the Win32 API REJECTS forward slashes — pass a native backslash path.
    $icon = $null; $bmp = $null; $ms = $null
    try {
        if (-not $script:ShellType) {
            $script:ShellType = Add-Type -MemberDefinition @'
[DllImport("shell32.dll", CharSet = CharSet.Unicode)]
public static extern IntPtr SHGetFileInfo(string pszPath, uint dwFileAttributes, out SHFILEINFO psfi, uint cbSizeFileInfo, uint uFlags);
[DllImport("user32.dll")]
public static extern bool DestroyIcon(IntPtr hIcon);
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct SHFILEINFO {
    public IntPtr hIcon;
    public int iIcon;
    public uint dwAttributes;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
    public string szDisplayName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 80)]
    public string szTypeName;
}
'@ -Name Shell -Namespace Win32 -PassThru
        }
        $native = $p -replace "/", "\"
        # SHGFI_ICON (0x100) | SHGFI_SHELLICONSIZE (0x4) = shell-size real icon
        $info = New-Object Win32.Shell+SHFILEINFO
        $res = [Win32.Shell]::SHGetFileInfo($native, 0, [ref]$info,
               [System.Runtime.InteropServices.Marshal]::SizeOf($info), 0x104)
        if ($res -eq [IntPtr]::Zero -or $info.hIcon -eq [IntPtr]::Zero) {
            # fallback: plain associated-icon extraction
            $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($p)
            if (-not $icon) { return $null }
            $bmp = $icon.ToBitmap()
        } else {
            $icon = [System.Drawing.Icon]::FromHandle($info.hIcon)
            $bmp = $icon.ToBitmap()
            [Win32.Shell]::DestroyIcon($info.hIcon) | Out-Null
        }
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        # plain return (byte[] unrolls to bytes on the pipeline) — caller
        # collects with @() and casts [byte[]], which is the reliable pattern
        return $ms.ToArray()
    } catch {
        return $null
    } finally {
        if ($ms) { $ms.Dispose() }
        if ($bmp) { $bmp.Dispose() }
    }
}

function Send-Json($ctx, [int]$code, $obj) {
    $body = ($obj | ConvertTo-Json -Depth 8)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $resp = $ctx.Response
    $resp.StatusCode = $code
    $resp.ContentType = "application/json; charset=utf-8"
    $resp.Headers.Add("Access-Control-Allow-Origin", "*")
    $resp.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    $resp.Headers.Add("Access-Control-Allow-Headers", "Content-Type")
    $resp.ContentLength64 = $bytes.Length
    $resp.OutputStream.Write($bytes, 0, $bytes.Length)
    $resp.OutputStream.Close()
}

function Send-Empty($ctx, [int]$code) {
    $resp = $ctx.Response
    $resp.StatusCode = $code
    $resp.Headers.Add("Access-Control-Allow-Origin", "*")
    $resp.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    $resp.Headers.Add("Access-Control-Allow-Headers", "Content-Type")
    $resp.ContentLength64 = 0
    $resp.OutputStream.Close()
}

function Handle-Request($ctx) {
    $req = $ctx.Request
    $method = $req.HttpMethod
    $path = $req.Url.AbsolutePath

    if ($method -eq "OPTIONS") { Send-Empty $ctx 204; return }
    if ($path -eq "/health") { Send-Json $ctx 200 @{ ok = $true; pid = $PID }; return }
    if ($path -eq "/apps") {
        Send-Json $ctx 200 @{ ok = $true; apps = @(Get-Registry) }
        return
    }
    if ($path -eq "/discover") {
        Send-Json $ctx 200 @{ ok = $true; apps = @(Get-Discovered) }
        return
    }
    if ($path -eq "/icon" -and $method -eq "GET") {
        $id = $req.QueryString["id"]
        $app = @(Get-Registry) | Where-Object { $_.id -eq $id } | Select-Object -First 1
        if (-not $app) { Send-Empty $ctx 204; return }
        [byte[]]$bytes = @(Get-IconBytes "$($app.path)")
        if (-not $bytes -or $bytes.Length -eq 0) { Send-Empty $ctx 204; return }
        $resp = $ctx.Response
        $resp.StatusCode = 200
        $resp.ContentType = "image/png"
        $resp.Headers.Add("Access-Control-Allow-Origin", "*")
        $resp.ContentLength64 = $bytes.Length
        $resp.OutputStream.Write($bytes, 0, $bytes.Length)
        $resp.OutputStream.Close()
        return
    }
    if ($method -eq "POST" -and ($path -eq "/add" -or $path -eq "/remove" -or $path -eq "/launch")) {
        $reader = New-Object System.IO.StreamReader($req.InputStream, [System.Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $reader.Close()
        try { $data = $body | ConvertFrom-Json } catch { Send-Json $ctx 400 @{ ok = $false; error = "bad json" }; return }

        if ($path -eq "/add") {
            $name = "$($data.name)".Trim()
            $p = ("$($data.path)".Trim() -replace "\\", "/")
            if (-not $name -or -not $p) { Send-Json $ctx 400 @{ ok = $false; error = "name+path required" }; return }
            $reg = @(Get-Registry)
            $existing = $reg | Where-Object { $_.path -ieq $p }
            if ($existing) { Send-Json $ctx 200 @{ ok = $true; app = $existing }; return }
            $app = [PSCustomObject]@{
                id     = [guid]::NewGuid().ToString("N")
                name   = $name
                path   = $p
                target = (Resolve-Target $p)
                added  = (Get-Date).ToString("yyyy-MM-dd HH:mm")
            }
            $reg += $app
            Save-Registry $reg
            Send-Json $ctx 200 @{ ok = $true; app = $app }
            return
        }
        if ($path -eq "/remove") {
            $id = "$($data.id)".Trim()
            $reg = @(Get-Registry) | Where-Object { $_.id -ne $id }
            Save-Registry $reg
            Send-Json $ctx 200 @{ ok = $true }
            return
        }
        if ($path -eq "/launch") {
            $id = "$($data.id)".Trim()
            $app = @(Get-Registry) | Where-Object { $_.id -eq $id } | Select-Object -First 1
            if (-not $app) { Send-Json $ctx 404 @{ ok = $false; error = "app not found" }; return }
            $target = $app.path
            if (-not (Test-Path $target)) { Send-Json $ctx 404 @{ ok = $false; error = "app not found on disk: $target" }; return }
            $p = Start-Process -FilePath $target -PassThru
            # launched from a background helper: the window opens but is NOT
            # activated (sits minimized/behind) — AppActivate brings it up
            try {
                Start-Sleep -Milliseconds 700
                $ws = New-Object -ComObject WScript.Shell
                $null = $ws.AppActivate($p.Id)
            } catch { }
            Send-Json $ctx 200 @{ ok = $true; launched = $app.name; pid = $p.Id }
            return
        }
    }
    Send-Json $ctx 404 @{ ok = $false; error = "not found: $path" }
}

function Start-Server {
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add($Base)
    try { $listener.Start() } catch {
        # port already in use -> another helper is running
        Write-Host "helper already running"
        return
    }
    Write-Host "AppVault Desktop Helper listening on $Base (pid $PID)"
    while ($true) {
        try {
            $ctx = $listener.GetContext()
            try { Handle-Request $ctx } catch { try { Send-Json $ctx 500 @{ ok = $false; error = "$($_.Exception.Message)" } } catch { } }
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
}

function Start-Hidden {
    # spawn the server hidden and exit (protocol-handler entry)
    $running = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $running) {
        Start-Process powershell.exe -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", "`"$ScriptPath`"", "serve"
        ) -WindowStyle Hidden
    }
}

# ── CLI modes ──
switch ($Mode) {
    "serve"    { Start-Server; break }
    "start"    { Start-Hidden; break }
    "list"     { @(Get-Registry) | ConvertTo-Json -Depth 6; break }
    "discover" { @(Get-Discovered) | ConvertTo-Json -Depth 6; break }
    "add"      {
        $reg = @(Get-Registry)
        $app = [PSCustomObject]@{ id = [guid]::NewGuid().ToString("N"); name = $Name; path = $Path; target = (Resolve-Target $Path); added = (Get-Date).ToString("yyyy-MM-dd HH:mm") }
        $reg += $app; Save-Registry $reg
        $app | ConvertTo-Json; break
    }
    "remove"   { Save-Registry (@(Get-Registry) | Where-Object { $_.id -ne $Id }); "removed $Id"; break }
    "launch"   {
        $app = @(Get-Registry) | Where-Object { $_.id -eq $Id } | Select-Object -First 1
        if ($app) { Start-Process -FilePath $app.path; "launched $($app.name)" } else { "not found" }
        break
    }
    default    { Start-Server }
}
