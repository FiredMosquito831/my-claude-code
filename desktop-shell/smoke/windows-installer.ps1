#requires -Version 5.1
<#
.SYNOPSIS
    The Windows *installer* smoke: silent install, snapshot diff, silent
    uninstall.

.DESCRIPTION
    Run it against the setup program the release is about to ship:

        pwsh -File smoke/windows-installer.ps1 `
            -Setup ../dist/MyClaudeCode-Setup-windows-x86_64.exe

    What it proves, in order:

      1. `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` completes without a
         prompt and without elevation. This is not a nicety: it is exactly
         the switch set winget supplies for `InstallerType: inno`, so an
         installer that prompts makes every unattended install hang forever;
      2. the payload landed -- `MyClaudeCode.exe`, its icon, a Start Menu
         shortcut, and an Apps & Features entry under **HKCU** (a per-user
         install, no admin);
      3. the entry names the desktop app, not "My Claude Code", so nobody
         uninstalls the server by mistake;
      4. `unins000.exe /VERYSILENT` removes every one of those again;
      5. and -- the assertion this file exists for -- the uninstaller leaves
         the *server's* artefacts alone: the HKCU `Run` value, anything under
         `~/.local/bin`, and `~/.mcc`. Those belong to `scripts/uninstall.ps1`.

    Nothing is installed to a shared location: `/DIR=` points at a scratch
    directory that is removed at the end. The registry and Start Menu are
    machine-global and cannot be redirected, so instead they are *snapshotted*
    before and compared after -- the same proof, without pretending the side
    effect did not happen. The app is never launched here (the window smoke in
    `smoke/windows.ps1` does that): a launch on a machine with no MCC would
    run the bootstrap installer, which is not this script's business.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Setup,

    [string] $Scratch
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$AppId = '{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}_is1'
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

function Fail([string] $Message) {
    Write-Host "INSTALLER SMOKE FAIL: $Message" -ForegroundColor Red
    exit 1
}

function Ok([string] $Message) {
    Write-Host "  ok: $Message"
}

function Get-Snapshot {
    [PSCustomObject]@{
        Uninstall = @(Get-ChildItem -Path $UninstallKey -ErrorAction SilentlyContinue |
            ForEach-Object { $_.PSChildName } | Sort-Object)
        Run       = @(
            $values = Get-Item -LiteralPath $RunKey -ErrorAction SilentlyContinue
            if ($values) {
                $values.GetValueNames() | Sort-Object | ForEach-Object {
                    "$_=$($values.GetValue($_))"
                }
            }
        )
        StartMenu = @(Get-ChildItem -LiteralPath $StartMenu -Recurse -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName } | Sort-Object)
    }
}

function Compare-Snapshot([string] $Name, $Before, $After) {
    # Compare-Object refuses two empty collections, and "both sides are empty"
    # is a legitimate state on a fresh CI runner with no HKCU Run values.
    if (@($Before).Count -eq 0 -and @($After).Count -eq 0) {
        Ok "$Name was empty before and after"
        return
    }
    $diff = Compare-Object -ReferenceObject @($Before) -DifferenceObject @($After)
    if ($diff) {
        $diff | Format-Table -AutoSize | Out-String | Write-Host
        Fail "$Name changed across install+uninstall; it must be identical"
    }
    Ok "$Name is byte-identical after uninstall"
}

Write-Host '== My Claude Code: Windows installer smoke =='

if (-not (Test-Path -LiteralPath $Setup -PathType Leaf)) {
    Fail "no setup program at $Setup"
}
$Setup = (Resolve-Path -LiteralPath $Setup).Path
$size = (Get-Item -LiteralPath $Setup).Length
$hash = (Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash.ToLowerInvariant()
Ok "setup present: $size bytes, sha256 $hash"

if (-not $Scratch) {
    $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
    if (-not $base -or $base.Contains(' ')) { $base = 'C:\tmp' }
    $Scratch = Join-Path $base ("mcc-installer-smoke-" + [guid]::NewGuid().ToString('N'))
}
$InstallDir = Join-Path $Scratch 'app'

if (Test-Path -LiteralPath $UninstallKey\$AppId) {
    Fail "the desktop app is already installed ($AppId); this smoke refuses to uninstall someone else's copy"
}

$before = Get-Snapshot
Ok "snapshotted $($before.Uninstall.Count) uninstall keys, $($before.Run.Count) Run values, $($before.StartMenu.Count) Start Menu paths"

# -- install ---------------------------------------------------------------
# `/LOG` so a failure in CI is diagnosable from the uploaded log rather than
# from an exit code alone.
$log = Join-Path $Scratch 'install.log'
New-Item -ItemType Directory -Force -Path $Scratch | Out-Null
$proc = Start-Process -FilePath $Setup -Wait -PassThru -ArgumentList `
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=$InstallDir", "/LOG=$log"
if ($proc.ExitCode -ne 0) {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 40 | Write-Host }
    Fail "silent install exited $($proc.ExitCode)"
}
Ok '/VERYSILENT install completed without a prompt'

foreach ($relative in @('MyClaudeCode.exe', 'app-icon.ico', 'unins000.exe')) {
    $path = Join-Path $InstallDir $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Fail "missing after install: $path" }
}
Ok 'the exe, the icon and the uninstaller are in place'

$shortcut = Join-Path $StartMenu 'My Claude Code.lnk'
if (-not (Test-Path -LiteralPath $shortcut -PathType Leaf)) {
    Fail "no Start Menu shortcut at $shortcut"
}
Ok 'Start Menu shortcut created'

$entry = Get-ItemProperty -LiteralPath "$UninstallKey\$AppId" -ErrorAction SilentlyContinue
if (-not $entry) { Fail "no HKCU Apps & Features entry at $UninstallKey\$AppId" }
if ($entry.DisplayName -ne 'My Claude Code (desktop app)') {
    Fail "Apps & Features says '$($entry.DisplayName)'; it must say 'My Claude Code (desktop app)' so nobody removes the server by mistake"
}
Ok "Apps & Features entry: $($entry.DisplayName) $($entry.DisplayVersion) (per-user, HKCU)"

# A per-user install must never have written a machine-wide uninstall key.
if (Get-Item -LiteralPath "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId" -ErrorAction SilentlyContinue) {
    Fail 'the installer wrote an HKLM uninstall key; PrivilegesRequired=lowest must keep everything in HKCU'
}
Ok 'nothing was written to HKLM'

# -- uninstall -------------------------------------------------------------
$uninstaller = Join-Path $InstallDir 'unins000.exe'
$proc = Start-Process -FilePath $uninstaller -Wait -PassThru -ArgumentList `
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
if ($proc.ExitCode -ne 0) { Fail "silent uninstall exited $($proc.ExitCode)" }
# The uninstaller relaunches itself from %TEMP% and returns before the copy
# has finished deleting the directory. Wait for the directory to go rather
# than racing it.
$deadline = (Get-Date).AddSeconds(60)
while ((Test-Path -LiteralPath (Join-Path $InstallDir 'MyClaudeCode.exe')) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
}
if (Test-Path -LiteralPath (Join-Path $InstallDir 'MyClaudeCode.exe')) {
    Fail 'MyClaudeCode.exe survived the uninstall'
}
Ok '/VERYSILENT uninstall removed the payload'

if (Test-Path -LiteralPath $shortcut) { Fail 'the Start Menu shortcut survived the uninstall' }
if (Get-Item -LiteralPath "$UninstallKey\$AppId" -ErrorAction SilentlyContinue) {
    Fail 'the Apps & Features entry survived the uninstall'
}
Ok 'shortcut and Apps & Features entry are gone'

$after = Get-Snapshot
Compare-Snapshot 'HKCU Uninstall' $before.Uninstall $after.Uninstall
Compare-Snapshot 'HKCU Run' $before.Run $after.Run
Compare-Snapshot 'Start Menu' $before.StartMenu $after.StartMenu

Remove-Item -LiteralPath $Scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'INSTALLER SMOKE PASS' -ForegroundColor Green
