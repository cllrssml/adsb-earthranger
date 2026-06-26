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

    do {
        $erToken = (Read-Host "  ER_TOKEN (EarthRanger bearer token)").Trim()
        if (-not $erToken) { Write-Host "  ER_TOKEN is required." -ForegroundColor Yellow }
    } while (-not $erToken)

    $adsbDefault  = "http://192.168.1.39:8080/data/aircraft.json"
    $pollDefault  = "10"
    $staleDefault = "30"

    $adsbUrl = (Read-Host "  ADSB_URL [$adsbDefault]").Trim()
    if (-not $adsbUrl) { $adsbUrl = $adsbDefault }

    $pollInterval = (Read-Host "  POLL_INTERVAL seconds [$pollDefault]").Trim()
    if (-not $pollInterval) { $pollInterval = $pollDefault }

    $staleThreshold = (Read-Host "  STALE_POS_THRESHOLD seconds [$staleDefault]").Trim()
    if (-not $staleThreshold) { $staleThreshold = $staleDefault }

    $envContent = "ER_SITE=$erSite`r`nER_TOKEN=$erToken`r`nADSB_URL=$adsbUrl`r`nPOLL_INTERVAL=$pollInterval`r`nSTALE_POS_THRESHOLD=$staleThreshold`r`n"
    [System.IO.File]::WriteAllText($envFile, $envContent, [System.Text.UTF8Encoding]::new($false))
    Write-OK ".env written to $envFile"
}

# ---------------------------------------------------------------------------
# 6. Write run.bat (avoids complex quoting in Task Scheduler argument)
# ---------------------------------------------------------------------------
$batFile = Join-Path $InstallDir "run.bat"
$batContent = "@echo off`r`ncd /d `"$InstallDir`"`r`n`"$python`" -u main.py >> `"$InstallDir\output.log`" 2>&1`r`n"
[System.IO.File]::WriteAllText($batFile, $batContent, [System.Text.Encoding]::ASCII)
Write-OK "run.bat written"

# ---------------------------------------------------------------------------
# 7. Register Task Scheduler task
# ---------------------------------------------------------------------------
Write-Step "Registering Windows Task Scheduler task..."
$taskName = "adsb-earthranger"
$logFile  = "$InstallDir\output.log"

$action = New-ScheduledTaskAction -Execute $batFile

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
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
