#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the ADS-B -> EarthRanger integration on Windows.
.DESCRIPTION
    Checks for Python (installs via winget if missing), downloads the latest
    release from GitHub, configures credentials, and registers a Task Scheduler
    task so the integration starts automatically at boot.
.PARAMETER InstallDir
    Destination folder. Defaults to C:\adsb-earthranger.
.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -InstallDir D:\integrations\adsb-earthranger
#>

param(
    [string]$InstallDir = "C:\adsb-earthranger"
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

function Write-Step { param([string]$msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "    [OK]  $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "    [!!]  $msg" -ForegroundColor Yellow }
function Write-Fail {
    param([string]$msg)
    Write-Host "`n    [XX]  $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  ADS-B -> EarthRanger  Installer" -ForegroundColor White
Write-Host "  ================================" -ForegroundColor White
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Find Python 3.10+
# ---------------------------------------------------------------------------
Write-Step "Checking Python 3.10+..."
$python = $null

foreach ($cmd in @("python", "python3")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
            $found = Get-Command $cmd -ErrorAction SilentlyContinue
            if ($found) {
                $python = $found.Source
                Write-OK "$ver  ($python)"
                break
            }
        }
    } catch {}
}

if (-not $python) {
    Write-Warn "Python 3.10+ not found -- installing Python 3.12 via winget..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Fail "winget not available. Install Python 3.10+ manually from https://python.org then re-run."
    }
    winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements

    # Refresh PATH for this session
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::Machine)
    $userPath    = [System.Environment]::GetEnvironmentVariable("Path", [System.EnvironmentVariableTarget]::User)
    $env:Path    = $machinePath + ";" + $userPath

    $candidate = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $candidate) {
        $python = $candidate
    } else {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) {
            $python = $found.Source
        } else {
            Write-Fail "Python install failed. Install Python 3.10+ from https://python.org then re-run."
        }
    }
    Write-OK "Python installed: $python"
}

# ---------------------------------------------------------------------------
# 2. Download latest release zip from GitHub
# ---------------------------------------------------------------------------
Write-Step "Downloading latest release from GitHub..."
$zipUrl  = "https://github.com/cllrssml/adsb-earthranger/archive/refs/heads/main.zip"
$zipPath = "$env:TEMP\adsb-earthranger-main.zip"
$tmpDir  = "$env:TEMP\adsb-earthranger-extract"

try {
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
} catch {
    Write-Fail "Download failed: $_"
}
Write-OK "Downloaded"

# ---------------------------------------------------------------------------
# 3. Extract to InstallDir
# ---------------------------------------------------------------------------
Write-Step "Installing to $InstallDir ..."
if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force

# GitHub zips have a single top-level folder, e.g. adsb-earthranger-main/
$inner = Get-ChildItem $tmpDir -Directory | Select-Object -First 1
if (-not $inner) { Write-Fail "Unexpected zip layout -- please report this issue." }

# Preserve existing .env if present (upgrade scenario)
$savedEnv = $null
$envFile  = Join-Path $InstallDir ".env"
if ((Test-Path $InstallDir) -and (Test-Path $envFile)) {
    $savedEnv = [System.IO.File]::ReadAllText($envFile)
    Write-Warn "Existing .env preserved (delete it and re-run to reconfigure)"
}

# Stop the scheduled task and kill all related processes before touching files.
# Stop-ScheduledTask halts the entry but leaves child processes running:
#   wscript.exe (run.vbs) -> cmd.exe (run.bat, holds output.log open) -> python.exe
# We must kill all three; python.exe command line has no path so match by name only.
Stop-ScheduledTask -TaskName "adsb-earthranger" -ErrorAction SilentlyContinue
Stop-Process -Name python  -Force -ErrorAction SilentlyContinue
Stop-Process -Name wscript -Force -ErrorAction SilentlyContinue
Get-WmiObject Win32_Process | Where-Object {
    $_.Name -eq "cmd.exe" -and $_.CommandLine -like "*$InstallDir*"
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
Move-Item $inner.FullName $InstallDir

if ($savedEnv) {
    [System.IO.File]::WriteAllText($envFile, $savedEnv, [System.Text.UTF8Encoding]::new($false))
}
Write-OK "Files in place"

# ---------------------------------------------------------------------------
# 4. Install Python dependencies
# ---------------------------------------------------------------------------
Write-Step "Installing Python dependencies..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r "$InstallDir\requirements.txt"
Write-OK "Dependencies installed"

# ---------------------------------------------------------------------------
# 5. Configure .env
# ---------------------------------------------------------------------------
$envFile = Join-Path $InstallDir ".env"

if (Test-Path $envFile) {
    Write-Step "Using existing .env -- skipping configuration prompts"
} else {
    Write-Step "Configuration"
    Write-Host "  Enter your details. Press Enter to accept [defaults] where shown.`n" -ForegroundColor Gray

    do {
        $erSite = (Read-Host "  ER_SITE  (e.g. https://your-site.pamdas.org)").Trim()
        if (-not $erSite) { Write-Host "  ER_SITE is required." -ForegroundColor Yellow }
    } while (-not $erSite)
    # Normalise: add https:// if user omitted the scheme
    if ($erSite -notmatch "^https?://") { $erSite = "https://$erSite" }
    $erSite = $erSite.TrimEnd("/")

    do {
        $erToken = (Read-Host "  ER_TOKEN (EarthRanger bearer token)").Trim()
        if (-not $erToken) { Write-Host "  ER_TOKEN is required." -ForegroundColor Yellow }
    } while (-not $erToken)

    $pollDefault  = "10"
    $staleDefault = "30"

    do {
        $adsbUrl = (Read-Host "  ADSB_URL  (e.g. http://192.168.x.x:8080/data/aircraft.json)").Trim()
        if (-not $adsbUrl) { Write-Host "  ADSB_URL is required." -ForegroundColor Yellow }
    } while (-not $adsbUrl)

    $pollInterval = (Read-Host "  POLL_INTERVAL seconds [$pollDefault]").Trim()
    if (-not $pollInterval) { $pollInterval = $pollDefault }

    $staleThreshold = (Read-Host "  STALE_POS_THRESHOLD seconds [$staleDefault]").Trim()
    if (-not $staleThreshold) { $staleThreshold = $staleDefault }

    $envContent = "ER_SITE=$erSite`r`nER_TOKEN=$erToken`r`nADSB_URL=$adsbUrl`r`nPOLL_INTERVAL=$pollInterval`r`nSTALE_POS_THRESHOLD=$staleThreshold`r`n"
    [System.IO.File]::WriteAllText($envFile, $envContent, [System.Text.UTF8Encoding]::new($false))
    Write-OK ".env written to $envFile"
}

# ---------------------------------------------------------------------------
# 6. Write run.bat and run.vbs
# ---------------------------------------------------------------------------
$batFile = Join-Path $InstallDir "run.bat"
$batContent = "@echo off`r`ncd /d `"$InstallDir`"`r`n`"$python`" -u main.py >> `"$InstallDir\output.log`" 2>&1`r`n"
[System.IO.File]::WriteAllText($batFile, $batContent, [System.Text.Encoding]::ASCII)
Write-OK "run.bat written"

# VBScript wrapper launches run.bat with a hidden window so no cmd console
# appears on the desktop. wscript.exe blocks until the script exits, which
# lets Task Scheduler track the process lifetime and restart on failure.
$vbsFile = Join-Path $InstallDir "run.vbs"
$vbsContent = "Dim wsh`r`nSet wsh = CreateObject(`"WScript.Shell`")`r`nwsh.Run Chr(34) & `"$batFile`" & Chr(34), 0, True`r`nSet wsh = Nothing`r`n"
[System.IO.File]::WriteAllText($vbsFile, $vbsContent, [System.Text.Encoding]::ASCII)
Write-OK "run.vbs written"

# ---------------------------------------------------------------------------
# 7. Register Task Scheduler task
# ---------------------------------------------------------------------------
Write-Step "Registering Windows Task Scheduler task..."
$taskName = "adsb-earthranger"
$logFile  = "$InstallDir\output.log"

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsFile`""

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId    "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel  Highest

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName  $taskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal | Out-Null

Write-OK "Task '$taskName' registered -- starts at boot, restarts on failure"

# ---------------------------------------------------------------------------
# 8. Start immediately
# ---------------------------------------------------------------------------
Write-Step "Starting the integration now..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName $taskName).State
Write-OK "Task state: $state"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Installation complete!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Monitor live output:" -ForegroundColor Gray
Write-Host "    Get-Content `"$logFile`" -Wait -Tail 20 -Encoding UTF8" -ForegroundColor White
Write-Host ""
Write-Host "  Task Scheduler commands:" -ForegroundColor Gray
Write-Host "    Stop  : Stop-ScheduledTask  -TaskName $taskName" -ForegroundColor White
Write-Host "    Start : Start-ScheduledTask -TaskName $taskName" -ForegroundColor White
Write-Host "    Status: (Get-ScheduledTask  -TaskName $taskName).State" -ForegroundColor White
Write-Host ""
Write-Host "  To uninstall:" -ForegroundColor Gray
Write-Host "    Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false" -ForegroundColor White
Write-Host "    Remove-Item `"$InstallDir`" -Recurse -Force" -ForegroundColor White
Write-Host ""
