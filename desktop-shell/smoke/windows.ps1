#requires -Version 5.1
<#
.SYNOPSIS
    The Windows release smoke: a real window, driven by a fake `mcc-desktop`.

.DESCRIPTION
    Run it against the binary the release is about to ship:

        pwsh -File smoke/windows.ps1 `
            -Binary src-tauri/target/x86_64-pc-windows-msvc/release/MyClaudeCode.exe

    What it proves, in order:

      1. the binary exists;
      2. the status contract, with its exit codes: the fake `mcc-desktop`
         answers `--print-status` with a `schema: 1` document and exit 0, and
         exits 3 for anything else;
      3. the real binary launches, runs that fake, and stays up;
      4. it starts nothing -- the document says `server_presence: foreign`, so
         the port-conflict page is the whole of the ladder and the fake
         `mcc-server` must never run;
      5. it exits when its exact process id is stopped.

    Nothing here touches a real configuration directory, a real server or a
    real port. `MCC_SHELL_DESKTOP_COMMAND`, `MCC_SHELL_SERVER_COMMAND` and
    `MCC_SHELL_DATA_DIR` point all three at a scratch directory that is
    removed at the end. No process is ever stopped by name, only by the id
    this script started.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Binary,

    # A directory whose path contains no spaces. `MCC_SHELL_DESKTOP_COMMAND`
    # is split on whitespace by the shell (so it can carry arguments), which
    # is what rules out "C:\Program Files\..." style paths here.
    [string] $Scratch
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# A port no MCC install would be listening on. Nothing is ever sent to it:
# `foreign` means the shell is *told* the port is taken and stops.
$SmokePort = 8199

function Fail([string] $Message) {
    Write-Host "SMOKE FAIL: $Message" -ForegroundColor Red
    exit 1
}

function Ok([string] $Message) {
    Write-Host "  ok: $Message"
}

Write-Host '== My Claude Code desktop shell: Windows release smoke =='

# -- 1. the artifact ------------------------------------------------------
if (-not (Test-Path -LiteralPath $Binary -PathType Leaf)) {
    Fail "no binary at $Binary"
}
$Binary = (Resolve-Path -LiteralPath $Binary).Path
$hash = (Get-FileHash -LiteralPath $Binary -Algorithm SHA256).Hash.ToLowerInvariant()
Ok "binary present: $hash  $(Split-Path -Leaf $Binary)"

if (-not $Scratch) {
    $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
    if (-not $base -or $base.Contains(' ')) { $base = 'C:\tmp' }
    $Scratch = Join-Path $base ("mcc-shell-smoke-" + [guid]::NewGuid().ToString('N'))
}
if ($Scratch.Contains(' ')) {
    Fail "the scratch directory must not contain a space: $Scratch"
}

New-Item -ItemType Directory -Force -Path $Scratch | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Scratch 'config\logs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Scratch 'data') | Out-Null

$statusPath = Join-Path $Scratch 'status.json'
$callsPath = Join-Path $Scratch 'desktop-calls.log'
$serverPath = Join-Path $Scratch 'server-started.log'
$stubPath = Join-Path $Scratch 'mcc-desktop.cmd'
$serverStub = Join-Path $Scratch 'mcc-server.cmd'
$outPath = Join-Path $Scratch 'shell.out'
$errPath = Join-Path $Scratch 'shell.err'

$configDir = (Join-Path $Scratch 'config') -replace '\\', '\\'
$serverLog = (Join-Path $Scratch 'config\logs\server.log') -replace '\\', '\\'

$status = @"
{
  "schema": 1,
  "version": "0.0.0-smoke",
  "config_dir": "$configDir",
  "config_dir_source": "current",
  "host": "127.0.0.1",
  "port": $SmokePort,
  "root_url": "http://127.0.0.1:$SmokePort",
  "admin_url": "http://127.0.0.1:$SmokePort/admin",
  "health_url": "http://127.0.0.1:$SmokePort/health",
  "server_presence": "foreign",
  "port_conflict": "Port $SmokePort on 127.0.0.1 is held by another process (pid 4242, smoke-stub). Stop it, or choose another port.",
  "server_mode": "spawn",
  "window": "auto",
  "window_open": true,
  "window_width": 900,
  "window_height": 700,
  "tray_enabled": true,
  "minimize_to_tray": false,
  "start_at_login": false,
  "server_log": "$serverLog",
  "start_timeout_seconds": 30.0,
  "health_check_interval_seconds": 0.5,
  "health_poll_seconds": 5.0,
  "health_failure_threshold": 3,
  "activation_poll_seconds": 1.0,
  "reconnect_timeout_seconds": 1320.0
}
"@
Set-Content -LiteralPath $statusPath -Value $status -Encoding UTF8

# `Command::new` on Windows cannot start a .cmd directly, which is why
# MCC_SHELL_DESKTOP_COMMAND below is "cmd /c <this file>" rather than the file
# on its own. The shell splits that override on whitespace and appends
# `--print-status`, so the batch sees the flag as %1.
$stub = @"
@echo off
>>"$callsPath" echo %*
if not "%~1"=="--print-status" (
  >&2 echo the smoke stub only answers --print-status
  exit /b 3
)
type "$statusPath"
exit /b 0
"@
Set-Content -LiteralPath $stubPath -Value $stub -Encoding ASCII

# If this ever runs, the shell started a server it was told not to start.
$serverBatch = @"
@echo off
>>"$serverPath" echo %*
ping -n 600 127.0.0.1 >nul
"@
Set-Content -LiteralPath $serverStub -Value $serverBatch -Encoding ASCII

# -- 2. the status contract, and its exit codes ---------------------------
$document = & cmd /c $stubPath --print-status
if ($LASTEXITCODE -ne 0) { Fail "the stub failed --print-status (exit $LASTEXITCODE)" }
Ok 'the stub answers --print-status with exit 0'

& cmd /c $stubPath --not-a-flag 2>$null | Out-Null
if ($LASTEXITCODE -ne 3) { Fail "expected exit 3 for an unknown flag, got $LASTEXITCODE" }
Ok 'the stub exits 3 for anything else'

$parsed = ($document -join "`n") | ConvertFrom-Json
if ($parsed.schema -ne 1) { Fail "schema was $($parsed.schema), not 1" }
if ($parsed.server_presence -ne 'foreign') { Fail "presence was $($parsed.server_presence)" }
foreach ($key in 'admin_url', 'health_url', 'config_dir', 'server_log', 'port_conflict') {
    if (-not $parsed.$key) { Fail "the document has no $key" }
}
Ok 'the document parses, schema 1, presence foreign'

# -- 3. the real binary, with a real window -------------------------------
$env:MCC_SHELL_DESKTOP_COMMAND = "cmd /c $stubPath"
$env:MCC_SHELL_SERVER_COMMAND = "cmd /c $serverStub"
$env:MCC_SHELL_DATA_DIR = (Join-Path $Scratch 'data')

$shell = Start-Process -FilePath $Binary -PassThru `
    -RedirectStandardOutput $outPath -RedirectStandardError $errPath
Ok "launched, pid $($shell.Id)"

$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Path -LiteralPath $callsPath)) {
    if ($shell.HasExited) {
        Get-Content -LiteralPath $errPath -ErrorAction SilentlyContinue | Write-Host
        Fail "the shell exited (code $($shell.ExitCode)) before reading a status document"
    }
    if ((Get-Date) -gt $deadline) {
        Get-Content -LiteralPath $errPath -ErrorAction SilentlyContinue | Write-Host
        Fail 'the shell never ran mcc-desktop --print-status (60s)'
    }
    Start-Sleep -Seconds 1
}
$calls = Get-Content -LiteralPath $callsPath
Ok "the shell ran the stub: $($calls[0])"
if (-not ($calls -match '--print-status')) {
    Fail 'the shell called mcc-desktop without --print-status'
}

# Let the ladder reach the port-conflict page and settle there.
Start-Sleep -Seconds 5
$shell.Refresh()
if ($shell.HasExited) {
    Get-Content -LiteralPath $errPath -ErrorAction SilentlyContinue | Write-Host
    Fail "the shell died after reading its status (exit $($shell.ExitCode))"
}
Ok 'the window is still up five seconds after the status was read'

# -- 4. it started nothing -------------------------------------------------
if (Test-Path -LiteralPath $serverPath) {
    Fail 'the shell started a server despite server_presence: foreign'
}
Ok 'no server was started (server_presence: foreign)'

# -- 5. it stops when told, by id -----------------------------------------
# By id, never by name: this machine may be running the operator's own copy.
Stop-Process -Id $shell.Id -Force -ErrorAction SilentlyContinue
if (-not $shell.WaitForExit(20000)) {
    Fail 'the shell did not exit within 20s'
}
Ok 'the shell exited when its process id was stopped'

Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host '== Windows smoke passed =='

# Explicit, and load-bearing. A PowerShell script with no `exit` inherits
# `$LASTEXITCODE` from the last native command it ran -- which here is the
# stub, deliberately exiting 3 -- so a passing smoke would report failure.
exit 0
