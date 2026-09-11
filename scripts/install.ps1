param(
    [string] $Version = "",
    [switch] $VoiceNim,
    [switch] $VoiceLocal,
    [switch] $VoiceAll,
    [string] $TorchBackend = "",
    [switch] $Rtk,
    [switch] $Desktop,
    [switch] $Restart,
    [switch] $NoStart,
    [switch] $DryRun,
    [switch] $Help,
    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]] $RemainingArgs = @()
)

# My Claude Code installer (Windows PowerShell 5.1+ and PowerShell 7+).
#
# This script owns every prerequisite the proxy needs, so a machine with
# nothing but Windows on it ends up with a working install:
#   * uv           -- installed from https://astral.sh/uv/install.ps1 when it is
#                     missing, and REPLACED when the uv already on PATH is older
#                     than the floor below. The floor is $MinUvVersion and it
#                     tracks [tool.uv] required-version in pyproject.toml.
#   * Python       -- $PythonVersion is downloaded by uv itself
#                     (`uv python install`) BEFORE the tool environment is
#                     built, and the tool environment is pinned to a uv-managed
#                     interpreter (`--managed-python`). A system Python is never
#                     used, and none needs to exist.
#   * My Claude Code -- installed into an isolated uv tool environment from the
#                     release wheel, after its SHA-256 is verified.
#
# Nothing has to be installed by hand first. The POSIX installer needs curl;
# here Invoke-RestMethod is part of PowerShell, so there is no such gap.
#
# Execution policy: the published one-liner pipes this script into the session
# (`irm ... | iex`) and so runs under the default RemoteSigned/Restricted
# policy without Set-ExecutionPolicy -- policy applies to script FILES, not to
# text executed in the current session. Only a saved copy run as
# `.\install.ps1` is subject to it; use
# `powershell -ExecutionPolicy Bypass -File .\install.ps1` for that.
#
# Behind a proxy, set $env:HTTPS_PROXY (and HTTP_PROXY/NO_PROXY) before running
# this script: Invoke-RestMethod/Invoke-WebRequest and uv both honour those
# variables, so every download here -- the uv installer, the Python build, the
# release wheel -- goes through it.
#
# Desktop app prerequisites are NOT installed here: the Windows desktop Setup
# .exe (Inno Setup) detects and bootstraps the WebView2 runtime itself, and the
# Linux .deb declares webkit2gtk in its Depends. -Desktop below only writes a
# Start Menu shortcut for the mcc-desktop command this install provides.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# uv colours output when it thinks stdout is a terminal, which PowerShell's
# capture looks like. Ask for plain text; Remove-AnsiEscape is the fallback.
$env:NO_COLOR = "1"

$FccRepo = "FiredMosquito831/my-claude-code"
$FccLatestReleaseUrl = "https://api.github.com/repos/$FccRepo/releases/latest"
$PythonVersion = "3.14.0"
$MinUvVersion = "0.11.0"
# What a complete install actually costs on disk, so a full-disk failure can
# say how much room to make instead of retrying. Measured on 2026-09-09 with a
# scratch UV_TOOL_DIR / UV_TOOL_BIN_DIR / UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR:
# see the commit message for the per-directory split. The recommendation is
# larger than the measurement because uv unpacks the interpreter and the wheel
# through its cache before it hardlinks them into place, so the peak is above
# the resting footprint.
$InstallFootprintMb = 340
$InstallRecommendedMb = 1024
$UvInstallUrl = "https://astral.sh/uv/install.ps1"
# Absolute path to the uv this script verified, set by Ensure-Uv. A fresh uv
# install lands in a directory the CURRENT shell may not have on PATH, so every
# later step runs this path rather than the bare name.
$script:UvPath = ""
# Set by Start-DeferredInstall when the app was running and the install was
# staged for completion after the user stops it.
$script:Deferred = $false
# Set by Invoke-RenameThenReinstall when the update completed immediately while
# launchers were open (old tool env renamed aside, fresh install in place).
$script:RenamedWhileRunning = $false
# Set by Invoke-StagedInstall: the commands whose shim was locked hard enough
# that the new launcher could not be placed. The OLD launcher stays, which is
# safe -- a uv shim is a version-agnostic stub that execs the interpreter at the
# canonical tool-dir path, and that path now holds the NEW install -- so these
# are reported as "refresh on the next install", never as failures.
$script:ShimsKeptInPlace = @()
# The update receipt this install shares with the deferred helper and with the
# desktop shell (src/my_claude_code/config/update_progress.py). EVERY ONE OF
# THESE FIVE MUST BE ASSIGNED HERE.
#
# `Set-StrictMode -Version Latest` above makes *retrieving* an unset variable a
# terminating error, and Write-InstallProgress's first act was
# `if (-not $script:InstallProgressPath)` against a variable nothing ever
# assigned. Its own `catch` -- there so that a receipt nobody can write is
# never the reason an install fails -- swallowed the error, so this script
# wrote NO RECEIPT AT ALL on Windows, silently, for the whole of 6.59.0 to
# 6.70.1. Measured on PowerShell 5.1 and pwsh 7 on 2026-09-10: the `updates`
# directory was not even created. The 6.59.0 contract ("a hand-run installer is
# visible to the helper-alive gate") was therefore inert, and on 2026-09-09 at
# 11:22 this script and the dashboard's helper installed over each other on the
# reporter's machine. The tests that "pinned" the receipt only grepped this
# file's text; `test_the_powershell_receipt_function_actually_writes_a_record`
# now RUNS it, under StrictMode, on both PowerShell editions.
$script:InstallProgressPath = ""
$script:InstallProgressLog = ""
$script:InstallProgressEncoding = $null
$script:InstallProgressStarted = 0
$script:InstallProgressVersion = ""
# How far through an episode the last record was. Stages are monotonic, so an
# episode never goes backwards and a window can draw them as a timeline.
$script:InstallProgressRank = 0
# 6.73.0's two extra receipt fields. `$null` means "this episode has not decided
# yet", which is what a reader shows nothing for; `$true`/`$false` is the
# answer to the only question that matters at the end of an update -- is a
# server answering on the configured port?
$script:InstallProgressRestarted = $null
$script:InstallProgressHolder = ""
# The exclusive update lock (decision Q5). Until 6.73.0 a hand-run install and
# a dashboard-triggered one shared nothing: they wrote the same receipt, into
# the same tool directory, with no coordination at all. At 15:04 on 2026-09-11
# a hand run erased the record of the update that had finished two minutes
# earlier, and on 2026-09-09 two installs wrote over each other's environment.
$script:HoldsUpdateLock = $false
$script:UpdateLockPath = ""
$script:UpdateLockOwner = $null
# What the caller asked for about the server. MCC_INSTALL_NO_START is the env
# form of -NoStart, for a caller that cannot add a switch -- the npm wrapper
# and install.cmd both pass arguments through a layer that has its own opinions
# about quoting.
$script:RestartRequested = $Restart.IsPresent
$script:NoStartRequested = ($NoStart.IsPresent -or ($env:MCC_INSTALL_NO_START -eq "1"))
# The first release whose `mcc-server` understands `--report-holder` and
# `--stop-holder`. Older builds do not REFUSE those flags: they ignore every
# argument but `--version` and start a server, which is why this gate exists at
# all rather than a try/catch around the call.
$RestartAwareVersion = "6.73.0"
$script:EnableRtk = $Rtk.IsPresent
$script:EnableDesktop = $Desktop.IsPresent
# Set by New-DesktopShortcut so the closing message reports what actually
# happened instead of hedging with "(if this succeeded)".
$script:DesktopShortcutPath = ""
$script:DesktopShortcutError = ""

function Show-Usage {
    @"
Usage: install.ps1 [options]

Installs or updates Free Claude Code to the latest published release.

Installs a compatible uv if one is missing. It does not install Claude Code,
Codex, or Pi -- install whichever of those you use yourself.

Options:
  -Version VALUE         Install this exact release instead of the latest.
  -VoiceNim              Install NVIDIA NIM voice transcription support.
  -VoiceLocal            Install local Whisper voice transcription support.
  -VoiceAll              Install all voice transcription backends.
  -TorchBackend VALUE    Use a uv PyTorch backend, such as cu130. Requires local voice.
  -Rtk                   Enable RTK token optimization for Claude Code, Codex, and Pi.
  -Desktop               Create a Start Menu shortcut for mcc-desktop.
                         The tray app needs the WebView2 runtime, which the
                         desktop Setup .exe bootstraps; Windows 11 ships it.
  -Restart               After a successful install, restart the My Claude
                         Code server on the port this configuration directory
                         is for: stop that one server by its exact process id,
                         start mcc-server again, and wait until it answers
                         /health. Every other My Claude Code server is listed
                         and left running.
  -NoStart               Never start a server, whatever else was asked. Same as
                         setting MCC_INSTALL_NO_START=1.
  -DryRun                Print commands without running them.
  -Help                  Show this help text.
"@
}

function Write-Step {
    param([string] $Message)

    Write-Host ""
    Write-Host "==> $Message"
}

function Format-Argument {
    param([string] $Value)

    if ($Value -match '^[A-Za-z0-9_./:@%+=,\[\]\\-]+$') {
        return $Value
    }

    return "'" + ($Value -replace "'", "''") + "'"
}

function Format-Command {
    param(
        [string] $FilePath,
        [string[]] $Arguments = @()
    )

    $parts = @($FilePath) + $Arguments
    return ($parts | ForEach-Object { Format-Argument ([string] $_) }) -join " "
}

function Invoke-NativeCommand {
    param(
        [string] $FilePath,
        [string[]] $Arguments = @(),
        [string] $CaptureTo = ""
    )

    $commandText = Format-Command -FilePath $FilePath -Arguments $Arguments
    Write-Host "+ $commandText"
    if ($DryRun) {
        return
    }

    # The transcript the desktop window tails. Every native command this
    # installer runs goes through here, so naming it here covers all of them.
    Write-InstallLog "+ $commandText"
    $global:LASTEXITCODE = 0
    # `Out-Host` on both branches: this function RUNS a command and shows what
    # it printed. It must not RETURN it.
    #
    # PowerShell returns everything a function writes to the output stream, so
    # while these branches passed the command's output down the pipeline,
    # `$InstalledVersion = Install-FreeClaudeCode` was the version string
    # PREFIXED BY EVERY LINE UV PRINTED -- an array -- and the next statement,
    # `Configure-AndConfirmFreeClaudeCode -ExpectedVersion $InstalledVersion`,
    # refused it with "Cannot convert value to type System.String". That is
    # every fresh Windows install through install.cmd / install.ps1 failing at
    # the verification step, and it has been failing since 6.64.0: the
    # install-smoke workflow only runs when `scripts/**` changes, so it was red
    # on main from 2026-09-08 (run 34285771201) with nothing to notice it.
    # `$renamed = Invoke-RenameThenReinstall ...` had the same latent shape.
    #
    # Out-Host writes to the console exactly as the passthrough did and emits
    # nothing, so the user still watches the install happen.
    if ([string]::IsNullOrWhiteSpace($CaptureTo)) {
        & $FilePath @Arguments | Out-Host
    }
    else {
        # uv's diagnosis of a failure is in the text it prints, never in its
        # exit code: a full disk and a locked file both come back as a non-zero
        # status and nothing else. The exception this function throws carries
        # only "Command failed with exit code N", which is why the installer
        # used to answer a full disk by retrying around locked files three
        # times. Tee uv's output to a file so the caller can read WHY, while
        # the user still watches the install happen.
        $previousPreference = $ErrorActionPreference
        # Windows PowerShell 5.1 turns a native command's stderr into an
        # ErrorRecord, and under "Stop" the FIRST line uv writes to stderr
        # would end the install before uv had finished. Capturing is a read.
        $ErrorActionPreference = "Continue"
        try {
            # The second tee is 6.71.0's, and it is deliberately an append per
            # line rather than a second Tee-Object: Tee-Object holds its file
            # open for the whole pipeline, which is fine for a capture nothing
            # reads until the end, and useless for a transcript another process
            # is tailing WHILE the install happens.
            & $FilePath @Arguments 2>&1 |
                ForEach-Object { $line = Convert-OutputLine $_; Write-InstallLog $line; $line } |
                Tee-Object -FilePath $CaptureTo |
                Out-Host
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
    }
    $exitCode = $LASTEXITCODE
    Write-InstallLog "exit $exitCode"
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $commandText"
    }
}

# How a uv failure is classified from the text uv printed. The same two tables
# exist in scripts/install.sh, and a contract test compares them, because the
# two installers must reach the same verdict about the same machine.
#
#   disk-full  the volume is out of space. Retrying cannot help, and the ladder
#              below (rename the tool dir aside, install through a staging
#              directory) writes MORE files, so it makes a full disk worse.
#   locked     a file is held by another process. This is what the ladder is
#              for, and it is the only thing the ladder is for.
#   unknown    everything else keeps the historical behaviour: try the ladder,
#              because a failure the ladder happens to fix is still a fixed
#              install, and then fail honestly.
$UvDiskFullSignatures = @(
    "os error 112",
    "not enough space on the disk",
    "no space left on device",
    "enospc"
)
$UvLockedSignatures = @(
    "os error 32",
    "access is denied",
    "being used by another process"
)

function Get-UvFailureCategory {
    param([string] $Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return "unknown"
    }
    $haystack = $Text.ToLowerInvariant()
    # Disk-full is tested first on purpose: when a message somehow carries both
    # shapes, the one the ladder cannot fix has to win.
    foreach ($signature in $UvDiskFullSignatures) {
        if ($haystack.Contains($signature)) {
            return "disk-full"
        }
    }
    foreach ($signature in $UvLockedSignatures) {
        if ($haystack.Contains($signature)) {
            return "locked"
        }
    }
    return "unknown"
}

function Read-CapturedOutput {
    param([string] $Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            return ""
        }
        return [IO.File]::ReadAllText($Path)
    }
    catch {
        return ""
    }
}

function New-CapturePath {
    return Join-Path ([IO.Path]::GetTempPath()) ("mcc-uv-" + [guid]::NewGuid().ToString("N") + ".log")
}

function Get-FreeSpaceMb {
    param([string] $Path)

    try {
        $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path))
        if ([string]::IsNullOrWhiteSpace($root)) {
            return $null
        }
        $drive = New-Object System.IO.DriveInfo($root)
        return [math]::Round($drive.AvailableFreeSpace / 1MB, 0)
    }
    catch {
        return $null
    }
}

function Get-InstallTargetDirectory {
    param([string] $UvPath)

    if (-not [string]::IsNullOrWhiteSpace($UvPath)) {
        try {
            $toolRoot = Invoke-NativeCapture -FilePath $UvPath -Arguments @("tool", "dir")
            if (-not [string]::IsNullOrWhiteSpace($toolRoot)) {
                return $toolRoot
            }
        }
        catch {
            # uv could not answer -- on a full disk that is entirely likely.
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($env:UV_TOOL_DIR)) {
        return $env:UV_TOOL_DIR
    }
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        return (Join-Path $env:APPDATA "uv")
    }
    return $PWD.Path
}

function Get-DiskFullMessage {
    param([string] $UvPath)

    $target = Get-InstallTargetDirectory -UvPath $UvPath
    $drive = ""
    try {
        $drive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($target))
    }
    catch {
        $drive = ""
    }
    if ([string]::IsNullOrWhiteSpace($drive)) {
        $drive = "the install drive"
    }
    $free = Get-FreeSpaceMb $target
    $freeText = if ($null -eq $free) { "could not be read" } else { "$free MB" }

    $lines = @(
        "",
        "The install stopped because $drive has no space left.",
        "  free on ${drive}: $freeText",
        "  this install needs: about $InstallFootprintMb MB (Python $PythonVersion, the tool environment and the uv cache it unpacks through); leave $InstallRecommendedMb MB free",
        "  it writes to: $target",
        "A full disk is not a locked file. Retrying writes more files, so the installer stops here instead.",
        "Free space on $drive and run the install command again.",
        "uv tool install --force removes the previous environment before it writes the new one, so this machine has no mcc-server until that re-run finishes."
    )
    return ($lines -join "`n")
}


function Remove-AnsiEscape {
    param([string] $Text)

    # uv colours its output when it believes stdout is a terminal. PowerShell's
    # capture does not look like a pipe to it, while POSIX $(...) does -- which
    # is why this only ever bit Windows. `uv tool dir --bin` came back as
    # ESC[36m + path + ESC[39m, so the path was 35 characters where the
    # directory name is 25: Test-Path failed and every "is this command inside
    # the tool bin?" comparison could never match.
    if ([string]::IsNullOrEmpty($Text)) {
        return $Text
    }
    return [regex]::Replace($Text, "\[[0-9;]*[A-Za-z]", "")
}

function Invoke-NativeCapture {
    param(
        [string] $FilePath,
        [string[]] $Arguments = @()
    )

    $commandText = Format-Command -FilePath $FilePath -Arguments $Arguments
    Write-Host "+ $commandText"
    $global:LASTEXITCODE = 0
    $output = & $FilePath @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $commandText"
    }

    # Strip colour before anything compares or path-tests this value.
    return (Remove-AnsiEscape (($output | Out-String).Trim())).Trim()
}

function Get-ApplicationCommand {
    param([string] $Name)

    $commands = @(Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
    if ($commands.Count -eq 0) {
        return $null
    }

    return $commands[0]
}

function Get-PowerShellExecutable {
    param([string] $PowerShellHome = $PSHOME)

    $executableName = if ($PSVersionTable.PSEdition -eq "Core") {
        "pwsh.exe"
    }
    else {
        "powershell.exe"
    }
    $bundledExecutable = Join-Path $PowerShellHome $executableName
    if (Test-Path -LiteralPath $bundledExecutable -PathType Leaf) {
        return $bundledExecutable
    }

    $pathCommand = Get-ApplicationCommand ([IO.Path]::GetFileNameWithoutExtension($executableName))
    if ($pathCommand) {
        return $pathCommand.Source
    }

    throw "Unable to locate a PowerShell executable for the downloaded installer."
}

function Add-PathEntry {
    param([string] $PathEntry)

    if ([string]::IsNullOrWhiteSpace($PathEntry)) {
        return
    }

    $separator = [IO.Path]::PathSeparator
    $entries = @()
    if (-not [string]::IsNullOrEmpty($env:Path)) {
        $entries = $env:Path -split [regex]::Escape([string] $separator)
    }

    if ($entries -notcontains $PathEntry) {
        $env:Path = "$PathEntry$separator$env:Path"
    }
}

function Add-KnownBinDirectories {
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        Add-PathEntry (Join-Path $env:USERPROFILE ".local\bin")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        Add-PathEntry (Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin")
        Add-PathEntry (Join-Path $env:LOCALAPPDATA "pi-node\current")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        Add-PathEntry (Join-Path $env:APPDATA "npm")
    }
}

function Add-PiBinDirectories {
    if ($DryRun) {
        return
    }

    Add-KnownBinDirectories
    $npm = Get-ApplicationCommand "npm"
    if (-not $npm) {
        return
    }

    $prefix = (& $npm.Source prefix -g 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($prefix)) {
        $prefix = (& $npm.Source config get prefix 2>$null | Out-String).Trim()
    }
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($prefix)) {
        Add-PathEntry $prefix
    }
}

function Invoke-DownloadedPowerShellInstaller {
    param(
        [string] $Url,
        [string] $Name,
        [switch] $NonInteractive
    )

    if ($DryRun) {
        Write-Host "+ irm $Url -OutFile <temporary-script>"
        $prefix = if ($NonInteractive) { "CODEX_NON_INTERACTIVE=1 " } else { "" }
        Write-Host "+ ${prefix}powershell -NoProfile -ExecutionPolicy Bypass -File <temporary-script>"
        return
    }

    $temporaryScript = Join-Path ([IO.Path]::GetTempPath()) ("fcc-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
    try {
        Write-Host "+ irm $Url -OutFile $(Format-Argument $temporaryScript)"
        Invoke-RestMethod -Uri $Url -OutFile $temporaryScript -ErrorAction Stop
        if ((-not (Test-Path -LiteralPath $temporaryScript)) -or ((Get-Item -LiteralPath $temporaryScript).Length -eq 0)) {
            throw "The downloaded $Name installer was empty."
        }

        $powerShellPath = Get-PowerShellExecutable

        $hadNonInteractive = Test-Path Env:CODEX_NON_INTERACTIVE
        $previousNonInteractive = $env:CODEX_NON_INTERACTIVE
        try {
            if ($NonInteractive) {
                $env:CODEX_NON_INTERACTIVE = "1"
            }
            Invoke-NativeCommand -FilePath $powerShellPath -Arguments @(
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                $temporaryScript
            )
        }
        finally {
            if ($hadNonInteractive) {
                $env:CODEX_NON_INTERACTIVE = $previousNonInteractive
            }
            else {
                Remove-Item Env:CODEX_NON_INTERACTIVE -ErrorAction SilentlyContinue
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $temporaryScript -Force -ErrorAction SilentlyContinue
    }
}

function Confirm-Application {
    param(
        [string] $CommandName,
        [string] $DisplayName
    )

    if ($DryRun) {
        Write-Host "+ $CommandName --version"
        return
    }

    $command = Get-ApplicationCommand $CommandName
    if (-not $command) {
        throw "$DisplayName was installed, but '$CommandName' is not available on PATH."
    }
    Invoke-NativeCommand -FilePath $command.Source -Arguments @("--version")
}

function Test-PiApplication {
    param($Command)

    try {
        $helpOutput = (& $Command.Source --help 2>$null | Out-String)
    }
    catch {
        return $false
    }
    return (
        $LASTEXITCODE -eq 0 -and
        $helpOutput.Contains("--extension") -and
        $helpOutput.Contains("--models")
    )
}

function Confirm-PiApplication {
    if ($DryRun) {
        Write-Host "+ pi --help (verify --extension and --models support)"
        Write-Host "+ pi --version"
        return
    }

    $command = Get-ApplicationCommand "pi"
    if (-not $command) {
        throw "Pi was installed, but 'pi' is not available on PATH."
    }
    if (-not (Test-PiApplication $command)) {
        throw "The 'pi' command at '$($command.Source)' is not a compatible Pi Coding Agent."
    }
    Invoke-NativeCommand -FilePath $command.Source -Arguments @("--version")
}

function Convert-UvVersionOutput {
    param([string] $Output)

    if ([string]::IsNullOrWhiteSpace($Output)) {
        return ""
    }

    if ($Output -match '(?m)(?:^|\s)(?:uv\s+)?(?<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)\b') {
        return $Matches["version"]
    }

    return ""
}

function Get-UvVersion {
    param([string] $UvPath)

    $output = Invoke-NativeCapture -FilePath $UvPath -Arguments @("--version")
    $version = Convert-UvVersionOutput $output
    if ([string]::IsNullOrWhiteSpace($version)) {
        throw "uv is present, but 'uv --version' did not return a valid version."
    }

    return $version
}

function Test-UvVersionAtLeast {
    param(
        [string] $Version,
        [string] $Minimum
    )

    $normalizedVersion = (Convert-UvVersionOutput $Version) -replace '[-+].*$', ''
    $normalizedMinimum = (Convert-UvVersionOutput $Minimum) -replace '[-+].*$', ''
    if ([string]::IsNullOrWhiteSpace($normalizedVersion) -or [string]::IsNullOrWhiteSpace($normalizedMinimum)) {
        throw "Unable to compare uv versions."
    }

    return ([version] $normalizedVersion) -ge ([version] $normalizedMinimum)
}

function Confirm-Uv {
    if ($DryRun) {
        Write-Host "+ uv --version"
        return
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if (-not $uvCommand) {
        throw "uv was installed, but it is not available on PATH."
    }

    $version = Get-UvVersion $uvCommand.Source
    if (-not (Test-UvVersionAtLeast -Version $version -Minimum $MinUvVersion)) {
        throw "uv $MinUvVersion or newer is required; found uv $version after installation."
    }
    $script:UvPath = $uvCommand.Source
    Write-Host "Verified uv $version."
}

function Ensure-Uv {
    if ($DryRun) {
        if (Get-ApplicationCommand "uv") {
            Write-Host "+ uv --version"
            Write-Host "A compatible existing uv will be left unchanged; an obsolete one will be replaced by the standalone installer."
        }
        else {
            Write-Host "uv is not installed; the current standalone uv would be installed."
            Invoke-DownloadedPowerShellInstaller -Url $UvInstallUrl -Name "uv"
            Confirm-Uv
        }
        return
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if ($uvCommand) {
        $version = Get-UvVersion $uvCommand.Source
        if (Test-UvVersionAtLeast -Version $version -Minimum $MinUvVersion) {
            $script:UvPath = $uvCommand.Source
            Write-Host "uv $version already satisfies >=$MinUvVersion; leaving it unchanged."
            return
        }
        Write-Host "uv $version is below $MinUvVersion; installing the current standalone uv."
    }
    else {
        Write-Host "uv is not installed; installing the current standalone uv."
    }

    Invoke-DownloadedPowerShellInstaller -Url $UvInstallUrl -Name "uv"
    Add-KnownBinDirectories
    Confirm-Uv
}

function Resolve-Release {
    if ($Version) {
        $resolvedVersion = $Version -replace '^v', ''
        $resolvedSha256 = ""
    }
    else {
        # A GET that changes nothing, so it also runs during -DryRun and can
        # report the version that would actually install.
        Write-Host "+ irm $FccLatestReleaseUrl"
        try {
            $release = Invoke-RestMethod -Uri $FccLatestReleaseUrl -Headers @{
                "Accept" = "application/vnd.github+json"
            } -ErrorAction Stop
        }
        catch {
            throw "Could not reach the release feed to find the latest version: $($_.Exception.Message)"
        }
        $resolvedVersion = ([string] $release.tag_name) -replace '^v', ''
        if ([string]::IsNullOrWhiteSpace($resolvedVersion)) {
            throw "Could not read the latest release version from the release feed."
        }
        $resolvedSha256 = ""
        # GitHub publishes a sha256 digest per asset, so the download is still
        # verified even though no checksum is pinned in this script.
        $wheelAsset = @($release.assets | Where-Object { $_.name -like "*.whl" })
        if ($wheelAsset.Count -gt 0 -and $wheelAsset[0].digest) {
            $resolvedSha256 = ([string] $wheelAsset[0].digest) -replace '^sha256:', ''
        }
    }
    $wheelName = "my_claude_code-$resolvedVersion-py3-none-any.whl"
    # Returned rather than stored in script scope: when this file is run as a
    # scriptblock (the published `irm | iex` form) a function's `$script:`
    # writes are not visible to the rest of the script.
    return [pscustomobject]@{
        Version   = $resolvedVersion
        WheelName = $wheelName
        WheelUrl  = "https://github.com/$FccRepo/releases/download/v$resolvedVersion/$wheelName"
        Sha256    = $resolvedSha256
    }
}

function Get-VerifiedReleaseWheel {
    param([Parameter(Mandatory = $true)] $Release)

    if ($DryRun) {
        Write-Host "+ irm $($Release.WheelUrl) -OutFile <temporary-wheel>"
        if ($($Release.Sha256)) {
            Write-Host "+ verify SHA-256 $($Release.Sha256) for <temporary-wheel>"
        }
        else {
            Write-Host "+ verify the SHA-256 published for this release"
        }
        return "<verified-release-wheel>"
    }

    $temporaryDirectory = Join-Path (
        [IO.Path]::GetTempPath()
    ) ("fcc-wheel-" + [guid]::NewGuid().ToString("N"))
    $wheelPath = Join-Path $temporaryDirectory $($Release.WheelName)
    try {
        New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
        Write-Host "+ irm $($Release.WheelUrl) -OutFile $(Format-Argument $wheelPath)"
        Invoke-RestMethod -Uri $($Release.WheelUrl) -OutFile $wheelPath -ErrorAction Stop
        if (
            (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) -or
            ((Get-Item -LiteralPath $wheelPath).Length -eq 0)
        ) {
            throw "The downloaded FCC release wheel was empty."
        }

        $actualSha256 = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash
        if ($($Release.Sha256)) {
            if ($actualSha256 -ne $($Release.Sha256)) {
                throw "FCC release wheel checksum mismatch; refusing to install."
            }
            Write-Host "Verified FCC v$($Release.Version) release wheel SHA-256."
        }
        else {
            # Only reachable with -Version, where the release feed was not read
            # and no published digest is available to compare against.
            Write-Host "FCC v$($Release.Version) release wheel SHA-256: $actualSha256"
        }
        return $wheelPath
    }
    catch {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Get-PackageSpec {
    param([string] $PackageUrl)

    $includeNim = $VoiceNim
    $includeLocal = $VoiceLocal

    if ($VoiceAll) {
        $includeNim = $true
        $includeLocal = $true
    }

    if ($includeNim -and $includeLocal) {
        return "my-claude-code[voice,voice_local] @ $PackageUrl"
    }
    if ($includeNim) {
        return "my-claude-code[voice] @ $PackageUrl"
    }
    if ($includeLocal) {
        return "my-claude-code[voice_local] @ $PackageUrl"
    }
    return "my-claude-code @ $PackageUrl"
}

function Install-ManagedPython {
    # Download $PythonVersion through uv before anything needs an interpreter,
    # so a machine with no Python at all installs cleanly. --no-bin and
    # --no-registry keep this to a self-contained interpreter under
    # UV_PYTHON_INSTALL_DIR: no python.exe shim is dropped into a bin directory
    # on PATH, and no PEP 514 entry is written to the Windows registry. The
    # tool environment finds it by version, not by PATH.
    $arguments = @("python", "install", "--no-bin", "--no-registry", $PythonVersion)

    if ($DryRun) {
        Write-Host "+ uv $($arguments -join ' ')"
        return
    }

    $uvPath = Resolve-UvPath "the Python installation"
    Invoke-NativeCommand -FilePath $uvPath -Arguments $arguments
}

function Resolve-UvPath {
    param([Parameter(Mandatory = $true)] [string] $Purpose)

    if (-not [string]::IsNullOrWhiteSpace($script:UvPath)) {
        return $script:UvPath
    }
    $uvCommand = Get-ApplicationCommand "uv"
    if (-not $uvCommand) {
        throw "uv is not available for $Purpose."
    }
    return $uvCommand.Source
}

function Install-FreeClaudeCode {
    $release = Resolve-Release
    $wheelPath = Get-VerifiedReleaseWheel -Release $release
    $packageUrl = if ($DryRun) {
        "file:///<verified-release-wheel>"
    }
    else {
        ([Uri]::new($wheelPath)).AbsoluteUri
    }
    $packageSpec = Get-PackageSpec -PackageUrl $packageUrl
    $arguments = @(
        "tool",
        "install",
        "--managed-python",
        "--force",
        "--refresh-package",
        "my-claude-code",
        "--python",
        $PythonVersion
    )
    if (-not [string]::IsNullOrWhiteSpace($TorchBackend)) {
        $arguments += @("--torch-backend", $TorchBackend)
    }
    $arguments += $packageSpec

    if ($DryRun) {
        return $release.Version
    }

    $uvPath = Resolve-UvPath "the Free Claude Code installation"

    $running = @(Get-RunningLaunchers)
    if ($running.Count -gt 0) {
        # Launchers are live. Windows refuses to DELETE a file or directory a
        # process runs from (the tool env's interpreter and loaded .pyd, and the
        # launcher's own .exe shim), so `uv tool install --force` fails partway.
        # But Windows ALLOWS RENAMING a running image, and a running process
        # keeps executing from the renamed file. So we rename the old tool env
        # AND every launcher shim aside, install fresh into the canonical paths,
        # and let uv write every entrypoint: open windows keep running the old
        # code, new windows/servers get the new version — exactly like POSIX.
        # If the tool-dir rename is refused (rare hard lock), fall back to the
        # detached-helper deferral.
        try {
            $toolDir = Get-UvToolDir -UvPath $uvPath
            if ($null -ne $toolDir) {
                $renamed = Invoke-RenameThenReinstall `
                    -UvPath $uvPath `
                    -Arguments $arguments `
                    -WheelPath $wheelPath `
                    -ToolDir $toolDir `
                    -Version $release.Version
                if ($renamed) {
                    return $release.Version
                }
                # Rename or install failed; fall back to the previous deferral.
            }
            return Start-DeferredInstall `
                -UvPath $uvPath `
                -Arguments $arguments `
                -WheelPath $wheelPath `
                -Running $running `
                -Version $release.Version
        }
        finally {
            # The wheel temp dir is consumed by whichever path ran; clean it up.
            Remove-Item -LiteralPath (Split-Path -Parent $wheelPath) -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $capturePath = New-CapturePath
    try {
        try {
            Invoke-NativeCommand -FilePath $uvPath -Arguments $arguments -CaptureTo $capturePath
        }
        catch {
            # Nothing of ours looked like it was running, yet uv could not write.
            # Process detection is a name match and a name match can miss (a
            # launcher started from a copy, a shim held by a scanner rather than
            # by us), so never let "os error 32" out of here without trying the
            # path built for locked files. Only a failure of THAT is a failure.
            #
            # But FIRST read what uv said. A machine that is simply out of space
            # used to take this branch too, and the ladder below then renamed the
            # tool directory aside and installed through a staging directory --
            # two more attempts, both writing files, on a volume with no room for
            # any of them. Three failures and eight minutes to say "the install
            # failed", when the first line uv printed said "os error 112".
            $category = Get-UvFailureCategory (Read-CapturedOutput $capturePath)
            if ($category -eq "disk-full") {
                Write-Host (Get-DiskFullMessage -UvPath $uvPath)
                exit 1
            }
            Write-Host "The install did not finish ($($_.Exception.Message)); retrying around locked files."
            $toolDir = Get-UvToolDir -UvPath $uvPath
            if ($null -eq $toolDir) {
                throw
            }
            $renamed = Invoke-RenameThenReinstall `
                -UvPath $uvPath `
                -Arguments $arguments `
                -WheelPath $wheelPath `
                -ToolDir $toolDir `
                -Version $release.Version
            if (-not $renamed) {
                throw
            }
        }
    }
    finally {
        Remove-Item -LiteralPath (Split-Path -Parent $wheelPath) -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $capturePath -Force -ErrorAction SilentlyContinue
    }
    return $release.Version
}

function Get-UvToolDir {
    param([Parameter(Mandatory = $true)] [string] $UvPath)

    $toolDir = Invoke-NativeCapture -FilePath $UvPath -Arguments @("tool", "dir")
    if ([string]::IsNullOrWhiteSpace($toolDir)) {
        return $null
    }
    return Join-Path $toolDir "my-claude-code"
}

function Invoke-PrecompileBytecode {
    <#
        .SYNOPSIS
        Write __pycache__ for the freshly installed tool environment.

        .DESCRIPTION
        Measured on this machine: the FIRST server start after an update costs
        about 3.5 seconds more than every later one, because CPython compiles
        every module it imports and writes the .pyc files as it goes. With
        releases arriving hourly, "the first start after an update" is most
        starts the user ever sees -- and it is the part of a start that happens
        before the port is bound, so it is the part they wait through with
        nothing on screen.

        `compileall -q` in the tool environment pays it once, here, where the
        user is already watching an installer. Best effort by design: a failure
        costs the 3.5 seconds back and nothing else, so it must never fail an
        install that otherwise worked.
    #>
    param([Parameter(Mandatory = $true)] [string] $UvPath)

    try {
        $toolDir = Get-UvToolDir -UvPath $UvPath
        if ([string]::IsNullOrWhiteSpace($toolDir)) {
            return
        }
        $sitePackages = Join-Path $toolDir "Lib\site-packages\my_claude_code"
        $python = Join-Path $toolDir "Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $sitePackages)) { return }
        if (-not (Test-Path -LiteralPath $python)) { return }
        Write-Host "Precompiling My Claude Code (saves a few seconds on the next start)..."
        & $python -m compileall -q $sitePackages *> $null
    }
    catch {
        # An optimisation, never a requirement.
    }
}

function Invoke-RenameThenReinstall {
    param(
        [Parameter(Mandatory = $true)] [string] $UvPath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $WheelPath,
        [Parameter(Mandatory = $true)] [string] $ToolDir,
        [Parameter(Mandatory = $true)] [string] $Version
    )

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"

    # uv writes the launcher shims (PE+zipapps) into the uv tool bin dir, and it
    # writes them in ASCII order of the file name *including* the ".exe" suffix
    # ("mcc-claude-old.exe" sorts before "mcc-claude.exe" because "-" < ".").
    # A running launcher holds its own .exe without FILE_SHARE_DELETE, so uv
    # cannot overwrite that one shim -- and uv ABORTS THE WHOLE INSTALL on the
    # first such failure, so every entrypoint alphabetically after it is never
    # written at all and no uv-receipt.toml is left behind. Measured with a
    # live `mcc-claude`: uv stopped at mcc-claude.exe and 16 of the 33
    # entrypoints were missing, with `uv tool list` reporting the tool as
    # malformed.
    #
    # Windows refuses to DELETE a running image but happily RENAMES one, and
    # the running process keeps executing from the renamed file. So rename
    # every launcher shim aside first: uv then writes all 33 entrypoints into
    # free paths and exits 0. Measured on a scratch UV_TOOL_BIN_DIR with a live
    # launcher: 17 shims renamed, 0 refused, uv exit 0, 33 shims present.
    $binDir = Invoke-NativeCapture -FilePath $UvPath -Arguments @("tool", "dir", "--bin")
    $canStage = -not [string]::IsNullOrWhiteSpace($binDir)
    $shimBackups = @()
    if ($canStage) {
        Remove-StaleShimBackup -BinDir $binDir
        $shimBackups = @(Rename-LauncherShimsAside -BinDir $binDir -Stamp $stamp -ToolDir $ToolDir)
    }
    # A rename can be REFUSED even for a shim whose only user is the process
    # running it: something else on the machine (an antivirus scan, the search
    # indexer, the shell reading the icon) can hold the file without
    # FILE_SHARE_DELETE for a moment. Measured: `mcc-desktop.exe` at 16:30 on
    # 2026-09-02 -- the tool-dir rename had already succeeded, so this function
    # was running, and uv still died with "failed to copy ... mcc-desktop.exe:
    # The process cannot access the file because it is being used by another
    # process (os error 32)". The refusal used to be swallowed and uv was let
    # loose on the locked path anyway; that is what turned one stuck shim into a
    # whole failed install. Now a refusal routes to the staged install below,
    # where uv never writes to a canonical path at all.
    $refusedShims = @($shimBackups | Where-Object { -not $_.Renamed })

    $renamed = ""
    if (Test-Path -LiteralPath $ToolDir -PathType Container) {
        # Best-effort sweep of stale .old-* dirs whose rename-lock is gone. A
        # dir still held open by a live window fails to delete; ignore it.
        Get-ChildItem -Path (Split-Path -Parent $ToolDir) -Directory -Filter "my-claude-code.old-*" -ErrorAction SilentlyContinue |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }

        $renamed = "$ToolDir.old-$stamp"
        try {
            Rename-Item -LiteralPath $ToolDir -NewName (Split-Path -Leaf $renamed) -ErrorAction Stop
        }
        catch {
            # The rename was refused (a process holds the dir without
            # share-delete). This is the one lock we cannot work around
            # in-process, so fall back to the deferred install rather than risk a
            # broken env. Put the shims back first: nothing was installed, so the
            # old ones are still the right ones.
            Restore-LauncherShim -Backups $shimBackups
            return $false
        }
    }

    # With the tool dir out of the way, uv installs into a clean canonical path.
    # It may still trip over a shim, so treat the direct run as the fast path and
    # the staged run as the one that cannot collide.
    $installed = $false
    $installError = ""
    # The direct install ALWAYS runs. It used to be skipped the moment a single
    # rename was refused -- and one `mcc-claude` window left open for the
    # afternoon is enough to refuse one, which on a real machine is the normal
    # case rather than the edge one. A refusal is the reason the staged install
    # sits BEHIND this one; it is not a reason to skip the cheap path, which
    # often succeeds anyway because the refusal was an antivirus scan or the
    # shell reading an icon and is gone by the time uv gets there.
    foreach ($move in $refusedShims) {
        Write-Host "Could not move $(Split-Path -Leaf $move.Original) aside: $($move.Error)"
    }
    if ($refusedShims.Count -gt 0) {
        # Say WHO. A refusal always has a holder, and naming it is the
        # difference between "the install retried for no reason" and "a server
        # from yesterday has never been stopped". Reporting only -- see
        # Write-McmHolderReport, which stops nothing.
        Write-McmHolderReport -Roots @($binDir, $ToolDir) -ToolRoot $ToolDir
    }
    $capturePath = New-CapturePath
    $diskFull = $false
    try {
        Invoke-NativeCommand -FilePath $UvPath -Arguments $Arguments -CaptureTo $capturePath
        $installed = $true
    }
    catch {
        $installError = $_.Exception.Message
        if ((Get-UvFailureCategory (Read-CapturedOutput $capturePath)) -eq "disk-full") {
            # The staging directory below is one more copy of the same files on
            # the same volume. Do not attempt it, and do not pretend the reason
            # was a lock.
            $diskFull = $true
        }
        if ($refusedShims.Count -gt 0) {
            $installError = "$installError ($($refusedShims.Count) launcher shim(s) could not be moved aside)"
        }
    }
    finally {
        Remove-Item -LiteralPath $capturePath -Force -ErrorAction SilentlyContinue
    }

    if ((-not $installed) -and $diskFull) {
        # Put the machine back the way it was before this function moved things
        # aside, then say the one true thing about it.
        Remove-Item -LiteralPath $ToolDir -Recurse -Force -ErrorAction SilentlyContinue
        if ((-not [string]::IsNullOrWhiteSpace($renamed)) -and (Test-Path -LiteralPath $renamed -PathType Container)) {
            Rename-Item -LiteralPath $renamed -NewName (Split-Path -Leaf $ToolDir) -ErrorAction SilentlyContinue
        }
        Restore-LauncherShim -Backups $shimBackups
        Write-Host (Get-DiskFullMessage -UvPath $UvPath)
        exit 1
    }

    if ((-not $installed) -and $canStage) {
        Write-Host "Installing through a staging directory instead ($installError)."
        try {
            $script:ShimsKeptInPlace = @(Invoke-StagedInstall `
                -UvPath $UvPath `
                -Arguments $Arguments `
                -BinDir $binDir `
                -ToolDir $ToolDir `
                -Stamp $stamp)
            $installed = $true
        }
        catch {
            $installError = $_.Exception.Message
        }
    }

    if (-not $installed) {
        # Everything was tried. Roll the old install back (dir and shims) so the
        # user is never left without a working tool, and report it rather than
        # pretending the install succeeded.
        Remove-Item -LiteralPath $ToolDir -Recurse -Force -ErrorAction SilentlyContinue
        if ((-not [string]::IsNullOrWhiteSpace($renamed)) -and (Test-Path -LiteralPath $renamed -PathType Container)) {
            Rename-Item -LiteralPath $renamed -NewName (Split-Path -Leaf $ToolDir) -ErrorAction SilentlyContinue
        }
        Restore-LauncherShim -Backups $shimBackups
        throw "My Claude Code install failed: $installError"
    }

    # New install succeeded. The old dir and the renamed-aside shims may still
    # be held open by a live window; remove them best-effort. Whatever is still
    # locked stays behind as orphaned garbage that the sweeps above reap on a
    # later install.
    if (-not [string]::IsNullOrWhiteSpace($renamed)) {
        Remove-Item -LiteralPath $renamed -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($canStage) {
        Remove-StaleShimBackup -BinDir $binDir
    }
    $script:RenamedWhileRunning = $true
    return $true
}

function Invoke-StagedInstall {
    # Install without ever letting uv write to a canonical shim path, then place
    # the shims ourselves. Returns the command names whose shim stayed as it was.
    #
    # uv aborts the whole install on the first entrypoint it cannot write, and
    # leaves no receipt behind, so a single locked .exe loses every command that
    # sorts after it. Point UV_TOOL_BIN_DIR at a fresh directory and that class
    # of failure disappears: uv writes all the shims and a complete receipt into
    # a place nothing can be holding. Copying them into the real bin directory
    # afterwards is per-file, so one stuck file costs exactly that one file.
    param(
        [Parameter(Mandatory = $true)] [string] $UvPath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $BinDir,
        [Parameter(Mandatory = $true)] [string] $ToolDir,
        [Parameter(Mandatory = $true)] [string] $Stamp
    )

    $stageBin = Join-Path ([IO.Path]::GetTempPath()) ("mcc-stage-bin-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stageBin | Out-Null
    $hadBinDirVariable = Test-Path Env:\UV_TOOL_BIN_DIR
    $previousBinDir = if ($hadBinDirVariable) { $env:UV_TOOL_BIN_DIR } else { "" }
    try {
        $env:UV_TOOL_BIN_DIR = $stageBin
        Invoke-NativeCommand -FilePath $UvPath -Arguments $Arguments
    }
    finally {
        if ($hadBinDirVariable) {
            $env:UV_TOOL_BIN_DIR = $previousBinDir
        }
        else {
            Remove-Item Env:\UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue
        }
    }

    # Place each staged shim. A destination that is still locked keeps the file
    # it already has: an old shim is a stub that execs the interpreter under the
    # canonical tool directory, and that directory now holds the new install, so
    # the old stub runs the new code. It is stale only in the sense that a
    # command ADDED by this release would be missing, which the verification
    # step reports honestly.
    $keptOld = @()
    foreach ($staged in @(Get-ChildItem -Path $stageBin -Filter "*.exe" -ErrorAction SilentlyContinue)) {
        $target = Join-Path $BinDir $staged.Name
        $asideName = $staged.Name + ".old-$Stamp-staged"
        $aside = Join-Path $BinDir $asideName
        $movedAside = $false
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            foreach ($attempt in 1..4) {
                try {
                    Rename-Item -LiteralPath $target -NewName $asideName -ErrorAction Stop
                    $movedAside = $true
                    break
                }
                catch {
                    Start-Sleep -Milliseconds (150 * $attempt)
                }
            }
        }
        try {
            Copy-Item -LiteralPath $staged.FullName -Destination $target -Force -ErrorAction Stop
        }
        catch {
            # Could not place the new shim. Put the old one back so the command
            # keeps working, and report it as refreshed-next-time.
            if ($movedAside -and (Test-Path -LiteralPath $aside -PathType Leaf)) {
                Move-Item -LiteralPath $aside -Destination $target -Force -ErrorAction SilentlyContinue
            }
            $keptOld += [IO.Path]::GetFileNameWithoutExtension($staged.Name)
        }
    }

    # Assigned away: this function's output IS the kept-shim list, and a stray
    # boolean on the pipeline would be reported to the user as a command name.
    $null = Update-UvReceiptEntrypoint -ToolDir $ToolDir -StageBinDir $stageBin -BinDir $BinDir
    Remove-Item -LiteralPath $stageBin -Recurse -Force -ErrorAction SilentlyContinue

    foreach ($name in $keptOld) {
        $name
    }
}

function Update-UvReceiptEntrypoint {
    # Point the receipt's entrypoints back at the real bin directory.
    #
    # Measured with uv 0.11.21: an install run with UV_TOOL_BIN_DIR set records
    # `install-path` under THAT directory, so a receipt left as uv wrote it
    # would send a later `uv tool uninstall`/`upgrade` at a temp path that no
    # longer exists. uv writes these paths with forward slashes; the rewrite is
    # a plain prefix substitution and lands through a temp file + Move-Item so a
    # crash can never leave a half-written receipt.
    param(
        [Parameter(Mandatory = $true)] [string] $ToolDir,
        [Parameter(Mandatory = $true)] [string] $StageBinDir,
        [Parameter(Mandatory = $true)] [string] $BinDir
    )

    $receipt = Join-Path $ToolDir "uv-receipt.toml"
    if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) {
        return $false
    }
    $realPrefix = $BinDir.Replace("\", "/").TrimEnd("/")
    $text = [IO.File]::ReadAllText($receipt)
    $updated = $text
    # Match whichever separator uv echoed back, so the rewrite does not depend
    # on how the staging path happened to be spelled.
    $backslashPrefix = $StageBinDir.Replace("/", "\").TrimEnd("\")
    foreach ($stagePrefix in @(
            $StageBinDir.Replace("\", "/").TrimEnd("/"),
            $backslashPrefix.Replace("\", "\\"),
            $backslashPrefix
        )) {
        $updated = $updated.Replace($stagePrefix, $realPrefix)
    }
    if ($updated -eq $text) {
        return $false
    }
    $temporary = "${receipt}.new"
    [IO.File]::WriteAllText(
        $temporary,
        $updated,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $temporary -Destination $receipt -Force
    return $true
}

function Get-ManagedShimName {
    # Every ".exe" in the uv tool bin dir that belongs to this tool.
    #
    # Deliberately NOT driven by Get-LauncherCommands alone. That list is a
    # hand-written contract, and a hand-written list is exactly what fell behind
    # before (mcc-desktop, mcc-rtk, mcc-help, mcc-anthropic-oauth-login were all
    # missing from it once). A shim we fail to move aside is a shim uv dies on,
    # so the set is built as a UNION of three independent sources and a new
    # command can never be missed by all three:
    #   1. the family pattern -- anything named mcc-*.exe / fcc-*.exe, plus the
    #      two distribution-named commands;
    #   2. whatever uv's OWN receipt for the tool calls an entrypoint;
    #   3. the Get-LauncherCommands contract list (which the contract test still
    #      holds equal to pyproject.toml -- the rename just no longer depends on
    #      it being right).
    param(
        [Parameter(Mandatory = $true)] [string] $BinDir,
        [string] $ToolDir = ""
    )

    $names = @{}
    $distributionCommands = @("my-claude-code.exe", "free-claude-code.exe")
    foreach ($file in @(Get-ChildItem -Path $BinDir -Filter "*.exe" -ErrorAction SilentlyContinue)) {
        if (($file.Name -match "^(mcc|fcc)-.+\.exe$") -or ($distributionCommands -contains $file.Name.ToLowerInvariant())) {
            $names[$file.Name] = $true
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($ToolDir)) {
        $receipt = Join-Path $ToolDir "uv-receipt.toml"
        if (Test-Path -LiteralPath $receipt -PathType Leaf) {
            foreach ($match in [regex]::Matches([IO.File]::ReadAllText($receipt), 'install-path\s*=\s*"([^"]+)"')) {
                $leaf = Split-Path -Leaf $match.Groups[1].Value
                if ($leaf -like "*.exe") {
                    $names[$leaf] = $true
                }
            }
        }
    }

    foreach ($commandName in Get-LauncherCommands) {
        $names["$commandName.exe"] = $true
    }

    foreach ($name in ($names.Keys | Sort-Object)) {
        $name
    }
}

function Rename-LauncherShimsAside {
    # Rename every shim this tool owns to "<name>.old-<stamp>" so uv can write a
    # fresh one at the canonical path even while a launcher is running from it.
    # Returns one record per shim -- including the ones that would NOT move, so
    # the caller can stage the install instead of walking into uv's abort.
    param(
        [Parameter(Mandatory = $true)] [string] $BinDir,
        [Parameter(Mandatory = $true)] [string] $Stamp,
        [string] $ToolDir = ""
    )

    $moves = @()
    foreach ($fileName in @(Get-ManagedShimName -BinDir $BinDir -ToolDir $ToolDir)) {
        $source = Join-Path $BinDir $fileName
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            continue
        }
        $backupName = $fileName + ".old-$Stamp"
        $move = [pscustomobject]@{
            Original = $source
            Backup   = (Join-Path $BinDir $backupName)
            Renamed  = $false
            Error    = ""
        }
        try {
            Rename-Item -LiteralPath $source -NewName $backupName -ErrorAction Stop
            $move.Renamed = $true
        }
        catch {
            $move.Error = $_.Exception.Message
        }
        $moves += $move
    }
    foreach ($move in $moves) {
        $move
    }
}

function Restore-LauncherShim {
    # Undo Rename-LauncherShimsAside after an install that did not happen.
    param([object[]] $Backups = @())

    foreach ($move in $Backups) {
        if (-not $move.Renamed) {
            continue
        }
        if (Test-Path -LiteralPath $move.Backup -PathType Leaf) {
            Move-Item -LiteralPath $move.Backup -Destination $move.Original -Force -ErrorAction SilentlyContinue
        }
    }
}

function Remove-StaleShimBackup {
    # Delete leftover "<name>.exe.old-<stamp>" shims from this and any earlier
    # run. One still held open by a live window refuses to delete; it is left
    # for the next install to reap.
    param([string] $BinDir = "")

    if ([string]::IsNullOrWhiteSpace($BinDir)) {
        return
    }
    Get-ChildItem -Path $BinDir -Filter "*.exe.old-*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
        }
}

function Enable-RtkForAgents {
    if (-not $script:EnableRtk) {
        return
    }

    Write-Step "Enabling RTK token optimization"
    if ($DryRun) {
        Write-Host "+ mcc-rtk enable claude,codex,pi"
        return
    }

    $rtkCommand = Get-ApplicationCommand "mcc-rtk"
    if ($rtkCommand) {
        Invoke-NativeCommand -FilePath $rtkCommand.Source -Arguments @("enable", "claude,codex,pi")
        return
    }

    $toolBin = Invoke-NativeCapture -FilePath (Get-ApplicationCommand "uv").Source -Arguments @("tool", "dir", "--bin")
    $rtkShim = Join-Path $toolBin "mcc-rtk.exe"
    Invoke-NativeCommand -FilePath $rtkShim -Arguments @("enable", "claude,codex,pi")
}

function Get-MccConfigDir {
    <#
        .SYNOPSIS
        Resolve the config directory this installation writes into.

        .DESCRIPTION
        The same three rungs the server itself walks, in the same order:
        an explicit MCC_CONFIG_DIR, then ~/.mcc, then a legacy ~/.fcc that an
        older install left behind. A hard-coded "$env:USERPROFILE\.mcc" here
        meant a scratch install with MCC_CONFIG_DIR pointed elsewhere still
        exported app-icon.ico into the REAL config home -- one file leaked out
        of the sandbox, every time.

        New installs still land in ~/.mcc: that is the last rung, and it is
        what an installation with neither directory present resolves to.
    #>

    if (-not [string]::IsNullOrWhiteSpace($env:MCC_CONFIG_DIR)) {
        return $env:MCC_CONFIG_DIR
    }
    $modern = Join-Path $env:USERPROFILE ".mcc"
    if (Test-Path -LiteralPath $modern) {
        return $modern
    }
    $legacy = Join-Path $env:USERPROFILE ".fcc"
    if (Test-Path -LiteralPath $legacy) {
        return $legacy
    }
    return $modern
}

function Get-MccEnvSetting {
    <#
        .SYNOPSIS
        One setting, as this installation's server would read it.

        .DESCRIPTION
        The process environment first -- which is what the server itself does,
        and what keeps a scratch install reading a scratch configuration --
        then `<config dir>/.env`.

        The installer READS. It never creates the file, never migrates a legacy
        directory and never writes a default back: an installer that repaired
        configuration would be a second `mcc-init`, and the one thing a restart
        must not do is change what the machine is configured to be while it is
        installing.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [string] $Default = ""
    )

    try {
        $fromEnvironment = [Environment]::GetEnvironmentVariable($Name)
        if (-not [string]::IsNullOrWhiteSpace($fromEnvironment)) {
            return $fromEnvironment.Trim()
        }
        $envFile = Join-Path (Get-MccConfigDir) ".env"
        if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
            return $Default
        }
        foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
            $text = ([string] $line).Trim()
            if ([string]::IsNullOrWhiteSpace($text)) { continue }
            if ($text.StartsWith("#")) { continue }
            if ($text.StartsWith("export ")) { $text = $text.Substring(7).Trim() }
            $split = $text.IndexOf("=")
            if ($split -lt 1) { continue }
            if ($text.Substring(0, $split).Trim() -ne $Name) { continue }
            $value = $text.Substring($split + 1).Trim()
            if ($value.Length -ge 2) {
                $quote = $value[0]
                if (($quote -eq '"' -or $quote -eq "'") -and $value[$value.Length - 1] -eq $quote) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
            return $value
        }
    }
    catch {
    }
    return $Default
}

function Get-MccServerAddress {
    <#
        .SYNOPSIS
        The host and port of the ONE server this install is for.

        .DESCRIPTION
        "Restart" means exactly one server: the one bound to the port of the
        configuration directory this install is for. Every other My Claude Code
        server -- another port, another configuration directory, the user's
        agent-serving instances -- is listed and never stopped.

        A HOST of 0.0.0.0 or :: is what the server BINDS, not an address a
        health probe can dial, so the reachable address is loopback in that
        case. That is also the address the server's own dashboard URL uses.
    #>

    $port = 8082
    $rawPort = Get-MccEnvSetting -Name "PORT" -Default "8082"
    $parsed = 0
    if ([int]::TryParse($rawPort, [ref] $parsed) -and $parsed -gt 0 -and $parsed -lt 65536) {
        $port = $parsed
    }
    $bindHost = Get-MccEnvSetting -Name "HOST" -Default "127.0.0.1"
    $reachable = $bindHost
    if ([string]::IsNullOrWhiteSpace($bindHost) -or $bindHost -eq "0.0.0.0" -or $bindHost -eq "::") {
        $reachable = "127.0.0.1"
    }
    return [pscustomobject]@{
        BindHost      = $bindHost
        ReachableHost = $reachable
        Port          = $port
    }
}

function Get-UpdateLockPath {
    <# .SYNOPSIS The one lock both update paths take, beside the receipt. #>

    return Join-Path (Join-Path (Get-MccConfigDir) "updates") "update.lock"
}

function Read-UpdateLockOwner {
    <#
        .SYNOPSIS
        Who holds the update lock, or $null when nobody does.

        .DESCRIPTION
        A lock file that exists but cannot be parsed is reported as held by an
        unknown owner rather than as absent: "I could not read it" must never
        be the reading that starts a second installer.
    #>
    param([string] $Path)

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            return $null
        }
        $raw = [System.IO.File]::ReadAllText($Path)
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return [pscustomobject]@{ pid = 0; started_display = ""; source = "an earlier installer" }
        }
        return ($raw | ConvertFrom-Json)
    }
    catch {
        return [pscustomobject]@{ pid = 0; started_display = ""; source = "an earlier installer" }
    }
}

function Test-UpdateLockOwnerAlive {
    <# .SYNOPSIS Whether the process that recorded the lock is alive. #>
    param($Owner)

    if ($null -eq $Owner) { return $false }
    $ownerPid = 0
    try { $ownerPid = [int] $Owner.pid } catch { $ownerPid = 0 }
    if ($ownerPid -le 0) { return $false }
    if ($ownerPid -eq $PID) { return $false }
    return [bool] (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue)
}

function Enter-UpdateLock {
    <#
        .SYNOPSIS
        Take the exclusive update lock, or say who has it. Returns $true/$false.

        .DESCRIPTION
        `CreateNew` with FileShare.None is the whole of the exclusion: the
        first writer to reach it wins and everyone else fails, atomically, on
        every Windows file system. A lock whose owner is GONE is reclaimed
        rather than waited on -- the pid decides, exactly as it decides for the
        helper-alive gate -- because the alternative is a crashed installer
        locking the machine out of updating for the rest of the day.
    #>

    if ($DryRun) { return $true }
    $path = Get-UpdateLockPath
    $script:UpdateLockPath = $path
    try {
        $updatesDir = Split-Path -Parent $path
        if (-not (Test-Path -LiteralPath $updatesDir)) {
            New-Item -ItemType Directory -Path $updatesDir -Force | Out-Null
        }
    }
    catch {
        # No updates directory means no lock and no receipt. An install that
        # cannot coordinate still installs; it just says so.
        return $true
    }
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        try {
            $stream = [System.IO.File]::Open($path, 'CreateNew', 'Write', 'None')
            try {
                $record = [ordered]@{
                    pid             = $PID
                    started_at      = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                    started_display = (Get-Date).ToString('HH:mm:ss')
                    source          = 'install.ps1'
                }
                $bytes = [System.Text.Encoding]::UTF8.GetBytes(($record | ConvertTo-Json -Compress))
                $stream.Write($bytes, 0, $bytes.Length)
            }
            finally {
                $stream.Dispose()
            }
            $script:HoldsUpdateLock = $true
            return $true
        }
        catch {
        }
        $owner = Read-UpdateLockOwner -Path $path
        if (Test-UpdateLockOwnerAlive -Owner $owner) {
            $script:UpdateLockOwner = $owner
            return $false
        }
        try { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue } catch { }
    }
    $script:UpdateLockOwner = (Read-UpdateLockOwner -Path $path)
    return $false
}

function Exit-UpdateLock {
    <# .SYNOPSIS Release the lock, if this process took it. Never throws. #>

    if (-not $script:HoldsUpdateLock) { return }
    $script:HoldsUpdateLock = $false
    try { Remove-Item -LiteralPath $script:UpdateLockPath -Force -ErrorAction SilentlyContinue } catch { }
}

function Write-WatchingInsteadNotice {
    <#
        .SYNOPSIS
        What a second installer prints instead of installing.

        .DESCRIPTION
        It does not queue and it does not install: it names the owner, points
        at the transcript that owner is writing, and exits 0. Two installers in
        one tool directory is the collision this lock exists to stop, and
        "wait for it" is a worse answer than "here is where to look" for a
        process that can take a quarter of an hour.
    #>
    param($Owner)

    $ownerPid = 0
    try { $ownerPid = [int] $Owner.pid } catch { $ownerPid = 0 }
    $started = ""
    try { $started = [string] $Owner.started_display } catch { $started = "" }
    Write-Host ""
    if ($ownerPid -gt 0 -and $started) {
        Write-Host "An update is already running (pid $ownerPid, started $started) -- watching it instead."
    }
    elseif ($ownerPid -gt 0) {
        Write-Host "An update is already running (pid $ownerPid) -- watching it instead."
    }
    else {
        Write-Host "An update is already running -- watching it instead."
    }
    $transcript = ""
    try {
        $progress = Join-Path (Join-Path (Get-MccConfigDir) "updates") "progress.json"
        if (Test-Path -LiteralPath $progress -PathType Leaf) {
            foreach ($line in [System.IO.File]::ReadAllLines($progress)) {
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                try {
                    $record = $line | ConvertFrom-Json
                    if ($record.log) { $transcript = [string] $record.log }
                }
                catch { }
            }
        }
    }
    catch { }
    if ($transcript) {
        Write-Host "It is writing: $transcript"
    }
}

function Get-PortHolderDocument {
    <#
        .SYNOPSIS
        Ask the installed product what holds a port, and whether it is ours.

        .DESCRIPTION
        The installer does not decide this. Which processes My Claude Code is
        allowed to stop is 6.59.0's and 6.72.2's rule, both of them Python, and
        a second opinion written in PowerShell is exactly how a product comes
        to stop something it should not have: the uv tool environment is a
        directory literally named `my-claude-code`, so every launcher's command
        line contains the product's name.

        `mcc-server --report-holder <port>` prints one JSON document and stops
        nothing. A build that predates the flag exits non-zero, and that is the
        answer this function returns $null for -- which the caller reads as
        "start nothing", never as "the port is free".
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Launcher,
        [Parameter(Mandatory = $true)][string] $ReachableHost,
        [Parameter(Mandatory = $true)][int] $Port
    )

    try {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $output = & $Launcher "--report-holder" "$Port" "--host" $ReachableHost 2>$null
        }
        finally {
            $ErrorActionPreference = $previous
        }
        if ($LASTEXITCODE -ne 0) { return $null }
        $text = (@($output) -join "`n").Trim()
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return ($text | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Stop-PortHolderServer {
    <#
        .SYNOPSIS
        Stop the ONE My Claude Code server holding this port, by its exact pid.

        .DESCRIPTION
        Same reasoning as Get-PortHolderDocument: the decision and the
        escalation both live in Python (`cli/installer_support.py`), which
        stops exactly the pids of that one launch, within the budget
        SERVER_GRACEFUL_SHUTDOWN_SECONDS configures, and refuses outright for
        anything that is not structurally one of our servers.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Launcher,
        [Parameter(Mandatory = $true)][string] $ReachableHost,
        [Parameter(Mandatory = $true)][int] $Port
    )

    try {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $output = & $Launcher "--stop-holder" "$Port" "--host" $ReachableHost 2>$null
        }
        finally {
            $ErrorActionPreference = $previous
        }
        if ($LASTEXITCODE -ne 0) { return $null }
        $text = (@($output) -join "`n").Trim()
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return ($text | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Write-OtherServerReport {
    <#
        .SYNOPSIS
        Name every OTHER My Claude Code server. None of them is ever stopped.

        .DESCRIPTION
        The user runs several servers on several ports with agents waiting on
        them. The installer's restart is one server -- the one on the port of
        the configuration directory this install is for -- and the rest exist
        in the transcript so that a machine with six of them is legible rather
        than mysterious.
    #>
    param($Document)

    if ($null -eq $Document) { return }
    $others = @()
    try { $others = @($Document.other_servers) } catch { $others = @() }
    if ($others.Count -eq 0) { return }
    Write-Host ""
    Write-Host "Other My Claude Code servers are running. None of them is touched:"
    foreach ($item in $others) {
        try { Write-Host ("  " + [string] $item.describe) } catch { }
    }
}

function Get-ServerStartTimeoutSeconds {
    <#
        .SYNOPSIS
        How long to wait for the restarted server to answer /health.

        .DESCRIPTION
        The desktop shell's own start budget, because it is the same question
        asked by a different watcher: DESKTOP_SERVER_START_TIMEOUT once per
        attempt, DESKTOP_SERVER_START_RETRIES attempts. A cold first start on
        this machine was measured at eighteen seconds, so the floor is not
        decorative.
    #>

    $timeout = 20.0
    $parsedTimeout = 0.0
    if ([double]::TryParse((Get-MccEnvSetting -Name "DESKTOP_SERVER_START_TIMEOUT" -Default "20"), [ref] $parsedTimeout)) {
        if ($parsedTimeout -gt 0) { $timeout = $parsedTimeout }
    }
    $retries = 2
    $parsedRetries = 0
    if ([int]::TryParse((Get-MccEnvSetting -Name "DESKTOP_SERVER_START_RETRIES" -Default "2"), [ref] $parsedRetries)) {
        if ($parsedRetries -gt 0) { $retries = $parsedRetries }
    }
    $budget = $timeout * $retries
    if ($budget -lt 30.0) { $budget = 30.0 }
    return $budget
}

function Test-VersionAtLeast {
    <#
        .SYNOPSIS
        Whether ``Version`` is at least ``Minimum``, compared numerically.

        .DESCRIPTION
        Numerically, so 6.73.10 sorts above 6.73.9, and "cannot parse it" is
        FALSE -- a version this cannot read must never be treated as new enough
        to be asked a question that an older build answers by starting a server.
    #>
    param(
        [string] $Version,
        [string] $Minimum
    )

    if ([string]::IsNullOrWhiteSpace($Version)) { return $false }
    $left = $null
    $right = $null
    try {
        $left = [System.Version]::Parse(($Version.Trim().TrimStart('v', 'V')))
        $right = [System.Version]::Parse($Minimum)
    }
    catch {
        return $false
    }
    return ($left -ge $right)
}

function Test-PortIsOccupied {
    <#
        .SYNOPSIS
        Whether this address can still be bound. Identifies nobody, stops nobody.

        .DESCRIPTION
        It exists for exactly one case: an installed `mcc-server` that predates
        `--report-holder` and so cannot classify a port holder. The only safe
        thing to know then is whether the port is FREE -- a free port is safe to
        start into; an occupied one is reported and left alone.

        A BIND, not a connect. A connect looked like the obvious test and is
        wrong on a real machine: measured here at 20:04, a TCP connect to a
        closed loopback port neither completed nor was refused -- the SYN was
        dropped -- so every port on the machine, including ones nothing was
        holding, read as "in use" and the installer refused to start anything.
        A bind asks the operating system the question directly, needs no round
        trip, and is the same question the server itself is about to ask.
        `ExclusiveAddressUse` matters for the same reason it matters in 6.59.0:
        without it Windows will happily let a second socket onto a live one.

        A bind that fails for ANY reason is "occupied". "I could not tell" must
        never be the reading that starts a second server onto somebody else's
        socket.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $ReachableHost,
        [Parameter(Mandatory = $true)][int] $Port
    )

    $listener = $null
    try {
        $address = [System.Net.IPAddress]::Loopback
        if ([string]::IsNullOrWhiteSpace($ReachableHost) -or $ReachableHost -eq '0.0.0.0') {
            $address = [System.Net.IPAddress]::Any
        }
        elseif (-not [System.Net.IPAddress]::TryParse($ReachableHost, [ref] $address)) {
            $address = [System.Net.IPAddress]::Loopback
        }
        $listener = New-Object System.Net.Sockets.TcpListener -ArgumentList $address, $Port
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
        return $false
    }
    catch {
        return $true
    }
    finally {
        if ($null -ne $listener) { try { $listener.Stop() } catch { } }
    }
}

function Wait-ForServerHealth {
    <#
        .SYNOPSIS
        Whether a listener on this address answers /health with 200, in budget.

        .DESCRIPTION
        THIS is the success condition of a restart. "The install exited 0" is
        not: on 2026-09-11 two installs exited 0 fifteen minutes apart and the
        user's server was down for both of them and after both of them.

        A 503 is not a failure here -- a starting server answers 503 with
        `x-mcc-starting: 1` while it binds (6.59.0) -- so anything that answers
        at all keeps the wait alive until the budget is spent.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Url,
        [Parameter(Mandatory = $true)][double] $BudgetSeconds
    )

    $deadline = (Get-Date).AddSeconds($BudgetSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ([int] $response.StatusCode -eq 200) {
                return $true
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

function Start-MccServerDetached {
    <#
        .SYNOPSIS
        Start mcc-server so that it outlives this installer, inherits this
        installer's configuration, and inherits none of its handles.

        .DESCRIPTION
        Three requirements at once, and each of them was learned the hard way on
        2026-09-11:

        1. **The caller's environment.** The server that starts has to be the
           server this install is for. The first attempt used
           `Win32_Process.Create`, which has no environment parameter at all:
           the process it creates gets the user's DEFAULT environment. With a
           scratch `MCC_CONFIG_DIR` and `PORT` set, the started server came up
           for the real configuration home on the real port -- and 6.59.0's
           `SERVER_PORT_TAKEOVER` then stopped the server already there. An
           installer must never be able to touch a server it was not asked
           about, so the configuration directory is also passed EXPLICITLY
           below rather than merely inherited.
        2. **No inherited handles.** `Start-Process -RedirectStandardOutput`
           asks .NET for `bInheritHandles=TRUE`, and on Windows that is
           all-or-nothing: the child inherits every inheritable handle this
           process holds, the STDOUT PIPE its own caller gave it included. The
           server keeps that pipe open for as long as it runs, the caller's read
           never reaches end-of-file, and the installer HANGS after a completely
           successful restart -- server answering, receipt written, `done` on
           disk. Every caller reads this script through a pipe: a GitHub `run:`
           step, `install.cmd`, `install.ps1 | tee`, the update helper.
        3. **No console window, and it outlives us.**

        `Start-Process` with NO `-Redirect*` switch is all three: PowerShell
        uses ShellExecute for it, which passes the caller's environment block
        and creates the process with `bInheritHandles=FALSE`. The redirection
        moves into a `cmd /c` instead -- and the outer pair of quotes around
        that command is not a typo: `cmd /c` strips the first and last quote of
        its argument when the argument starts with one.

        MCC_OPEN_BROWSER is deliberately not touched (invariant 8): whatever the
        configuration says is what the started server does, exactly as if the
        user had typed `mcc-server`.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Launcher,
        [Parameter(Mandatory = $true)][string] $StdOutPath,
        [Parameter(Mandatory = $true)][string] $StdErrPath
    )

    # A one-line batch file rather than a quoted command line. `cmd /c` and
    # `Start-Process` disagree about quoting in a way that cost two measured
    # failures here on 2026-09-11 -- a stripped outer quote that ran nothing at
    # all (19:53), and an argument mangled into an INTERACTIVE cmd that printed
    # its banner into the start log and never started a server (19:57). A file
    # has no quoting rules, and it can be read afterwards to see exactly what
    # was run.
    $configDir = Get-MccConfigDir
    $runner = Join-Path (Split-Path -Parent $StdOutPath) ("start-server-" + [System.IO.Path]::GetFileNameWithoutExtension($StdOutPath) + ".cmd")
    $lines = @(
        "@echo off",
        # The configuration directory EXPLICITLY, not merely inherited. The
        # restart means the server of the directory this install is for, and on
        # 2026-09-11 a start that lost it came up for a different configuration
        # home -- and 6.59.0's port takeover then stopped the server that was
        # already there.
        ('set "MCC_CONFIG_DIR=' + $configDir + '"'),
        ('"' + $Launcher + '" > "' + $StdOutPath + '" 2> "' + $StdErrPath + '"')
    )
    [System.IO.File]::WriteAllText($runner, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.ASCIIEncoding))
    # No -Redirect* switch, so PowerShell uses ShellExecute: the child gets this
    # process's ENVIRONMENT and none of its HANDLES. Both halves matter. See the
    # .DESCRIPTION above.
    $process = Start-Process `
        -FilePath $runner `
        -WorkingDirectory ([System.IO.Path]::GetTempPath()) `
        -WindowStyle Hidden `
        -PassThru
    return [pscustomobject]@{ Id = [int] $process.Id }
}

function Get-ChildFailureDetail {
    <# .SYNOPSIS The last few lines a failed child wrote, for the receipt. #>
    param([string] $Path, [int] $Lines = 12)

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
        $all = @([System.IO.File]::ReadAllLines($Path) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($all.Count -eq 0) { return "" }
        $tail = $all
        if ($all.Count -gt $Lines) { $tail = $all[($all.Count - $Lines)..($all.Count - 1)] }
        return ($tail -join [Environment]::NewLine)
    }
    catch {
        return ""
    }
}

function Invoke-RestartAfterInstall {
    <#
        .SYNOPSIS
        Stop the one server this install is for, start the new one, prove it.

        .DESCRIPTION
        The order, and why each step is where it is:

          1. Read the port and host of the configuration directory this install
             is for. Not "the default port" and not "every MCC port".
          2. Ask the product what holds that port. An MCC server is ours to
             stop; anything else is reported and left alone, and so is a port
             whose holder could not be identified.
          3. Stop it by exact pid, within its own configured budget, and wait
             for the port to come free.
          4. Start `mcc-server` detached and hidden.
          5. Wait for /health to answer 200. THIS is success. If it never
             answers, say so plainly -- V1 has no staged swap in the installer,
             so the previous version is no longer on disk and the honest
             instruction is to run the installer again.

        Returns $true when a listener is answering on the configured port.
    #>
    param([Parameter(Mandatory = $true)][string] $InstalledVersion)

    $address = Get-MccServerAddress
    $script:InstallProgressHolder = ""
    $healthUrl = "http://$($address.ReachableHost):$($address.Port)/health"
    Write-Step "Restarting the My Claude Code server on port $($address.Port)"
    Write-InstallLog ("Restart requested for the server on " + $address.ReachableHost + ":" + $address.Port + ".")

    $binDir = ""
    try {
        $binDir = Invoke-NativeCapture -FilePath (Resolve-UvPath "the restart") -Arguments @("tool", "dir", "--bin")
    }
    catch {
        $binDir = ""
    }
    $launcher = $null
    if (-not [string]::IsNullOrWhiteSpace($binDir)) {
        $launcher = Get-LauncherInBinDirectory -BinDir $binDir -Name "mcc-server"
    }
    if (-not $launcher) {
        $command = Get-ApplicationCommand "mcc-server"
        if ($command) { $launcher = $command.Source }
    }
    if (-not $launcher) {
        $message = "The server was not restarted: mcc-server was not found after the install."
        Write-Host $message
        Write-InstallProgress -Stage 'failed' -Message $message
        return $false
    }

    # ---- who holds the port -------------------------------------------------
    # ONLY of a build that has the question. `cli.entrypoints.serve` ignores
    # every argument but `--version`, so a 6.72.2 or older `mcc-server
    # --report-holder 8392` does not fail -- it STARTS A SERVER on the
    # configured port, and the installer waiting for its answer blocks behind it
    # for ever. Measured on the real installer at 20:12 on 2026-09-11. The
    # version is the one thing every build has always answered.
    $report = $null
    if (Test-VersionAtLeast -Version $InstalledVersion -Minimum $RestartAwareVersion) {
        $report = Get-PortHolderDocument -Launcher $launcher -ReachableHost $address.ReachableHost -Port $address.Port
    }
    else {
        Write-InstallLog ("Installed version " + $InstalledVersion + " predates --report-holder; not asking it.")
    }
    if ($null -eq $report) {
        # The installed mcc-server predates --report-holder (a pinned -Version,
        # or the first install of this release, whose wheel is the one BEFORE
        # it). It cannot classify the port holder, and this script must not try:
        # deciding which processes My Claude Code may stop in PowerShell is the
        # second opinion that has no business existing.
        #
        # What it CAN do without classifying anything is ask whether the port is
        # occupied at all -- a TCP connect, which stops nothing and identifies
        # nothing. A free port is safe to start into; an occupied one is
        # reported and left exactly as it is.
        if (Test-PortIsOccupied -ReachableHost $address.ReachableHost -Port $address.Port) {
            $message = "Port $($address.Port) is in use and this build of mcc-server cannot say by what, so nothing was stopped and nothing was started. Run the installer again once this version is installed, or stop the server yourself and start it with: mcc-server"
            Write-Host $message
            Write-InstallLog $message
            $script:InstallProgressRestarted = $false
            Write-InstallProgress -Stage 'done' -Message $message
            return $false
        }
        Write-Host "Nothing is listening on port $($address.Port); starting the server."
        Write-InstallLog "The installed build cannot classify a port holder, and nothing holds the port; starting."
        return (Start-AndProveServer -Launcher $launcher -InstalledVersion $InstalledVersion -HealthUrl $healthUrl -Port $address.Port)
    }
    Write-OtherServerReport -Document $report
    $holderDescription = ""
    try { $holderDescription = [string] $report.holder_description } catch { $holderDescription = "" }
    $holderIsOurs = $false
    try { $holderIsOurs = [bool] $report.holder.is_mcc_server } catch { $holderIsOurs = $false }
    $holderPid = 0
    try { if ($null -ne $report.holder.pid) { $holderPid = [int] $report.holder.pid } } catch { $holderPid = 0 }
    $script:InstallProgressHolder = $holderDescription

    if ($holderPid -gt 0 -and (-not $holderIsOurs)) {
        # Invariant 1, in the one place it matters most: a foreign holder of
        # the port is never killed, by any path.
        $reason = ""
        try { $reason = [string] $report.holder.reason } catch { $reason = "" }
        $message = "Port $($address.Port) is held by $holderDescription, which is not a My Claude Code server. Nothing was stopped and nothing was started."
        Write-Host ""
        Write-Host $message
        if ($reason) { Write-Host "  ($reason)" }
        Write-InstallLog $message
        $script:InstallProgressRestarted = $false
        Write-InstallProgress -Stage 'done' -Message $message
        return $false
    }

    # ---- stop exactly that one server --------------------------------------
    if ($holderPid -gt 0) {
        Write-InstallProgress -Stage 'stopping' -Message "Stopping the server on port $($address.Port)."
        Write-Host "Stopping $holderDescription."
        $stopped = Stop-PortHolderServer -Launcher $launcher -ReachableHost $address.ReachableHost -Port $address.Port
        $stopMessage = ""
        try { $stopMessage = [string] $stopped.message } catch { $stopMessage = "" }
        if ($stopMessage) {
            Write-Host $stopMessage
            Write-InstallLog $stopMessage
        }
        $portFree = $false
        try { $portFree = [bool] $stopped.port_free } catch { $portFree = $false }
        if (-not $portFree) {
            $message = "The server on port $($address.Port) could not be stopped, so nothing was started. $stopMessage"
            Write-Host $message
            Write-InstallLog $message
            $script:InstallProgressRestarted = $false
            Write-InstallProgress -Stage 'failed' -Message $message
            return $false
        }
    }
    else {
        Write-Host "Nothing was listening on port $($address.Port); starting the server."
        Write-InstallLog "Nothing held the port; starting the server."
    }

    # ---- start the new one --------------------------------------------------
    return (Start-AndProveServer -Launcher $launcher -InstalledVersion $InstalledVersion -HealthUrl $healthUrl -Port $address.Port)
}

function Start-AndProveServer {
    <#
        .SYNOPSIS
        Start mcc-server detached and wait for /health. Returns $true when a
        listener answers.

        .DESCRIPTION
        The second half of the restart, in a function of its own because two
        paths reach it -- the ordinary one and the fallback for an installed
        build that cannot classify a port holder -- and a second copy of "start
        it and prove it" is a second definition of success.

        THIS is the success condition. "The install exited 0" is not: on
        2026-09-11 two installs exited 0 fifteen minutes apart and the machine
        had no server through either of them.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Launcher,
        [Parameter(Mandatory = $true)][string] $InstalledVersion,
        [Parameter(Mandatory = $true)][string] $HealthUrl,
        [Parameter(Mandatory = $true)][int] $Port
    )

    Write-InstallProgress -Stage 'starting' -Message "Starting My Claude Code $InstalledVersion."
    $updatesDir = Join-Path (Get-MccConfigDir) "updates"
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $startOut = Join-Path $updatesDir ("server-start-" + $stamp + ".log")
    $startErr = Join-Path $updatesDir ("server-start-" + $stamp + ".err.log")
    $child = $null
    try {
        $child = Start-MccServerDetached -Launcher $Launcher -StdOutPath $startOut -StdErrPath $startErr
    }
    catch {
        $message = "The server could not be started: $($_.Exception.Message) The previous version is no longer installed; run the installer again."
        Write-Host $message
        Write-InstallLog $message
        $script:InstallProgressRestarted = $false
        Write-InstallProgress -Stage 'failed' -Message $message
        return $false
    }
    Write-Host "Started mcc-server (pid $($child.Id)). Waiting for it to answer $HealthUrl."
    Write-InstallLog ("Started mcc-server, pid " + $child.Id + "; waiting for " + $HealthUrl + ".")

    # ---- prove it -----------------------------------------------------------
    if (Wait-ForServerHealth -Url $HealthUrl -BudgetSeconds (Get-ServerStartTimeoutSeconds)) {
        $script:InstallProgressRestarted = $true
        $message = "My Claude Code $InstalledVersion is installed and answering on port $Port."
        Write-Host $message
        Write-InstallLog $message
        Write-InstallProgress -Stage 'done' -Message $message
        return $true
    }

    # The child is detached and this process is not its parent, so there is no
    # exit code to read from a handle we do not hold. What there IS: whether the
    # pid is still alive, and everything it wrote before it stopped.
    $exitCode = "the process did not exit"
    try {
        if (-not (Get-Process -Id $child.Id -ErrorAction SilentlyContinue)) {
            $exitCode = "the process exited (see its output below)"
        }
    }
    catch { $exitCode = "unknown" }
    $detail = Get-ChildFailureDetail -Path $startErr
    if (-not $detail) { $detail = Get-ChildFailureDetail -Path $startOut }
    $message = "The new server did not answer $HealthUrl. Exit code: $exitCode. The previous version is no longer installed; run the installer again."
    Write-Host ""
    Write-Host $message
    Write-Host "Its output is in: $startErr"
    if ($detail) {
        Write-Host "Last lines:"
        Write-Host $detail
    }
    Write-InstallLog $message
    if ($detail) { Write-InstallLog $detail }
    $script:InstallProgressRestarted = $false
    Write-InstallProgress -Stage 'failed' -Message $message
    return $false
}

function Write-InstallProgress {
    <#
        .SYNOPSIS
        Append one liveness record to the update receipt this machine shares.

        .DESCRIPTION
        The same file, the same fields and the same sentences the deferred
        update helper writes (src/my_claude_code/application/release_updates.py
        and src/my_claude_code/config/update_progress.py). Until 6.59.0 only
        the helper wrote it, so a hand-run `irm install.ps1 | iex` was invisible
        to every reader: the desktop shell saw no installer in flight and was
        free to start one of its own into the tool directory this script is
        writing -- two installers, one target, which is exactly the collision
        the helper-alive gate was added to prevent.

        Never throws. A receipt nobody can write must not be the reason an
        install fails.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Stage,
        [Parameter(Mandatory = $true)][string] $Message
    )

    if ($DryRun) {
        # A dry run changes nothing, so it must not claim an installer is
        # running: a reader that believed it would refuse to install for the
        # next fifteen minutes.
        return
    }
    try {
        Initialize-InstallProgress
        if (-not $script:InstallProgressPath) {
            return
        }
        # Monotonic, exactly as the deferred helper is: an episode only moves
        # forward, so a window can draw the records as a timeline. A stage this
        # table does not know is written rather than dropped.
        $rank = Get-InstallStageRank $Stage
        if ($rank -eq 0) {
            $rank = $script:InstallProgressRank
        }
        if ($rank -lt $script:InstallProgressRank) {
            return
        }
        $script:InstallProgressRank = $rank
        $elapsed = [math]::Round(
            [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 - $script:InstallProgressStarted, 3)
        if ($elapsed -lt 0) {
            $elapsed = 0
        }
        $record = [ordered]@{
            stage            = $Stage
            message          = $Message
            at               = (Get-Date).ToUniversalTime().ToString('o')
            parent           = 0
            helper_pid       = $PID
            started_at       = $script:InstallProgressStarted
            elapsed_seconds  = $elapsed
            helper_done      = ($Stage -in @('done', 'failed', 'recovered'))
            version          = $script:InstallProgressVersion
            log              = $script:InstallProgressLog
            source           = 'install.ps1'
            # What this episode did about the server. `restarted` is $null
            # until a restart is attempted, so "we never tried" and "we tried
            # and failed" are different answers rather than the same false.
            restarted        = $script:InstallProgressRestarted
            holder           = $script:InstallProgressHolder
        }
        $line = ($record | ConvertTo-Json -Compress) + [Environment]::NewLine
        [System.IO.File]::AppendAllText($script:InstallProgressPath, $line, $script:InstallProgressEncoding)
    }
    catch {
    }
}

function Get-InstallStageRank {
    <#
        .SYNOPSIS
        How far through an episode a stage is; 0 for one this build does not
        know. The same table as config/update_progress.py's
        UPDATE_PROGRESS_STAGE_ORDER, and a contract test compares them.
    #>
    param([Parameter(Mandatory = $true)][string] $Stage)

    switch ($Stage) {
        # Rank 0: the marker that OPENS an episode, written before any work.
        # The monotonic guard reads rank 0 as "keep the rank you had", so a
        # marker never blocks the stage that follows it.
        'episode' { return 0 }
        'waiting-for-parent' { return 1 }
        'staging' { return 2 }
        'stopping' { return 3 }
        'installing' { return 4 }
        'verifying' { return 5 }
        'swapping' { return 6 }
        'starting' { return 7 }
        'handing-off' { return 7 }
        'rolling-back' { return 8 }
        'done' { return 9 }
        'failed' { return 9 }
        'recovered' { return 9 }
        default { return 0 }
    }
}

function Initialize-InstallProgress {
    <#
        .SYNOPSIS
        Open this episode's receipt and transcript, once. Never throws.

        .DESCRIPTION
        MCC_INSTALL_LOG is how a caller that already owns a transcript -- the
        deferred update helper, which starts this script for its own recovery
        ladder -- gets ONE file for the whole episode instead of two half
        stories. Unset, this install opens its own `install-<stamp>.log` beside
        the receipt, which is what a hand-run `irm install.ps1 | iex` wants.
    #>

    if ($script:InstallProgressPath) {
        return
    }
    try {
        $updatesDir = Join-Path (Get-MccConfigDir) "updates"
        if (-not (Test-Path -LiteralPath $updatesDir)) {
            New-Item -ItemType Directory -Path $updatesDir -Force | Out-Null
        }
        $script:InstallProgressEncoding = New-Object System.Text.UTF8Encoding($false)
        $script:InstallProgressStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $shared = ""
        if (Test-Path Env:\MCC_INSTALL_LOG) {
            $shared = [string] $env:MCC_INSTALL_LOG
        }
        if ($shared) {
            # Someone else's episode. Append to it; do NOT truncate it.
            $script:InstallProgressLog = $shared
        }
        else {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $script:InstallProgressLog = Join-Path $updatesDir ("install-" + $stamp + ".log")
            [System.IO.File]::WriteAllText($script:InstallProgressLog, '', $script:InstallProgressEncoding)
        }
        $script:InstallProgressPath = Join-Path $updatesDir "progress.json"
        # APPEND. Until 6.73.0 this line truncated the receipt, and at 15:04 on
        # 2026-09-11 that erased the whole record of the update helper that had
        # finished two minutes earlier -- while a desktop window was supposed
        # to be reading it. An episode is opened by a MARKER record instead, so
        # a reader that arrives a minute late can still find where the current
        # episode begins (decision Q5).
        #
        # Written here rather than through Write-InstallProgress because that
        # function's first act is to call this one.
        $marker = [ordered]@{
            stage           = 'episode'
            message         = 'An update started.'
            at              = (Get-Date).ToUniversalTime().ToString('o')
            parent          = 0
            helper_pid      = $PID
            started_at      = $script:InstallProgressStarted
            elapsed_seconds = 0
            helper_done     = $false
            version         = $script:InstallProgressVersion
            log             = $script:InstallProgressLog
            source          = 'install.ps1'
            restarted       = $null
            holder          = ''
        }
        $markerLine = ($marker | ConvertTo-Json -Compress) + [Environment]::NewLine
        [System.IO.File]::AppendAllText($script:InstallProgressPath, $markerLine, $script:InstallProgressEncoding)
    }
    catch {
        $script:InstallProgressPath = ""
    }
}

function Convert-OutputLine {
    <#
        .SYNOPSIS
        One line of a native command's merged output, as text.

        .DESCRIPTION
        `2>&1` turns every stderr line into an ErrorRecord, whose default
        string form is sometimes the exception's TYPE NAME rather than what was
        written -- "System.Management.Automation.RemoteException" appeared in
        the middle of uv's own diagnostics on 2026-09-11. The message is the
        line the command actually printed.
    #>
    param([object] $Value)

    if ($null -eq $Value) {
        return ""
    }
    if ($Value -is [System.Management.Automation.ErrorRecord]) {
        return [string] $Value.Exception.Message
    }
    return [string] $Value
}

function Write-InstallLog {
    <#
        .SYNOPSIS
        Append one line to this episode's installer transcript, as it happens.

        .DESCRIPTION
        One AppendAllText per line rather than a held stream, so a reader in
        another process -- the desktop window, which tails this file every tick
        -- sees the line the moment it exists, and so a killed install does not
        truncate what it already said. Never throws.
    #>
    param([string] $Text)

    if ($DryRun) {
        return
    }
    try {
        Initialize-InstallProgress
        if (-not $script:InstallProgressLog) {
            return
        }
        $stampNow = (Get-Date).ToUniversalTime().ToString('HH:mm:ss')
        # An ErrorRecord is what `2>&1` makes of a stderr line, and its default
        # string form is sometimes the exception's type name rather than what
        # was written. The message is the line the command actually printed.
        $body = if ($null -eq $Text) { "" }
            elseif ($Text -is [System.Management.Automation.ErrorRecord]) { [string] $Text.Exception.Message }
            else { [string] $Text }
        [System.IO.File]::AppendAllText(
            $script:InstallProgressLog,
            ("[" + $stampNow + "] " + $body + [Environment]::NewLine),
            $script:InstallProgressEncoding)
    }
    catch {
    }
}

function New-DesktopShortcut {
    if (-not $script:EnableDesktop) {
        return
    }

    Write-Step "Creating a Start Menu shortcut"
    if ($DryRun) {
        Write-Host "+ export app-icon.ico and create a Start Menu shortcut for mcc-desktop"
        return
    }

    try {
        $mccDesktopCommand = Get-ApplicationCommand "mcc-desktop"
        if ($mccDesktopCommand) {
            $launcherPath = $mccDesktopCommand.Source
        }
        else {
            $uvCommand = Get-ApplicationCommand "uv"
            $toolBin = Invoke-NativeCapture -FilePath $uvCommand.Source -Arguments @("tool", "dir", "--bin")
            $launcherPath = Join-Path $toolBin "mcc-desktop.exe"
        }

        $configDir = Get-MccConfigDir
        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
        $iconPath = Join-Path $configDir "app-icon.ico"

        # mcc-desktop is a [project.gui-scripts] entry, so mcc-desktop.exe is a
        # GUI-subsystem binary and PowerShell's call operator does NOT wait for
        # it. Invoke-NativeCommand would return in ~0.5s with LASTEXITCODE 0
        # while the icon is still being written, and IconLocation below would
        # point at a file that does not exist yet -- a shortcut with a blank
        # icon, reported as success. Start-Process -Wait is what actually waits.
        $export = Start-Process -FilePath $launcherPath `
            -ArgumentList @("--export-icon", $iconPath) `
            -Wait -PassThru -NoNewWindow
        $iconReady = ($export.ExitCode -eq 0) -and (Test-Path $iconPath)
        if (-not $iconReady) {
            Write-Warning "Could not export the app icon; the shortcut will use the default icon."
        }

        $startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
        New-Item -ItemType Directory -Path $startMenuDir -Force | Out-Null
        $shortcutPath = Join-Path $startMenuDir "My Claude Code.lnk"

        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $launcherPath
        # A missing icon must never cost the user the shortcut itself.
        if ($iconReady) {
            $shortcut.IconLocation = $iconPath
        }
        $shortcut.Description = "My Claude Code"
        $shortcut.Save()

        $script:DesktopShortcutPath = $shortcutPath
        Write-Host "Created Start Menu shortcut: $shortcutPath"
    }
    catch {
        $script:DesktopShortcutError = $_.Exception.Message
        Write-Warning "Could not create the Start Menu shortcut: $($_.Exception.Message)"
    }
}

function Configure-AndConfirmFreeClaudeCode {
    param([Parameter(Mandatory = $true)] [string] $ExpectedVersion)

    if ($DryRun) {
        Write-Host "+ uv tool update-shell"
        Write-Host "+ uv tool dir --bin"
        Write-Host "+ verify mcc-server, mcc-claude, mcc-codex, mcc-pi, mcc-help, and my-claude-code in the uv tool bin directory"
        Write-Host "+ mcc-server --version"
        return
    }

    $uvPath = Resolve-UvPath "PATH configuration"
    Invoke-NativeCommand -FilePath $uvPath -Arguments @("tool", "update-shell")
    $toolBin = Invoke-NativeCapture -FilePath $uvPath -Arguments @("tool", "dir", "--bin")
    if ([string]::IsNullOrWhiteSpace($toolBin)) {
        throw "uv returned an empty tool bin directory."
    }

    Add-PathEntry $toolBin
    $toolBinPath = ([IO.Path]::GetFullPath($toolBin)).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    # Verify EVERY command the wheel publishes -- the same list the running
    # launcher detection uses, so there is exactly one list to keep in step with
    # pyproject.toml and a contract test that fails on drift.
    #
    # This used to check a shorter hand-written list, and skipped the check
    # entirely whenever a shim could not be replaced, then printed "installed
    # and verified" on the strength of `mcc-server --version` alone. That check
    # cannot fail: the shims are version-agnostic launchers, so an OLD shim
    # reports the NEW version. A user was told the install was verified while
    # seven of their commands did not exist. Never report success for a command
    # that is not there.
    #
    # Ask the DIRECTORY, never PATH. This used to call Get-ApplicationCommand --
    # `Get-Command -CommandType Application`, i.e. the FIRST hit on PATH -- and
    # throw when the file it found was not in the uv tool bin directory. On
    # Windows npm's global bin (%APPDATA%\npm) precedes ~/.local/bin, so a
    # leftover `my-claude-code.cmd` from an older version of the npm package
    # made this check decide that a complete, correct install had put its files
    # somewhere illegal -- permanently, on every later run of the one-liner too.
    # Which program a NAME resolves to is a fact about PATH order on the user's
    # machine; it is not a failed install. install.sh has always tested the file
    # (`[ ! -x "$tool_bin/$command_name" ]`) and was immune to the whole class.
    $installedCommands = @{}
    $missingCommands = @()
    $shadowedCommands = @()
    foreach ($commandName in Get-LauncherCommands) {
        $launcher = Get-LauncherInBinDirectory -BinDir $toolBinPath -Name $commandName
        if ($null -eq $launcher) {
            $missingCommands += $commandName
            continue
        }
        $installedCommands[$commandName] = $launcher

        $shadow = Get-ShadowingProgram -Name $commandName -BinDir $toolBinPath
        if ($null -ne $shadow) {
            $shadowedCommands += [pscustomobject]@{ Name = $commandName; Path = $shadow }
        }
    }

    if ($missingCommands.Count -gt 0) {
        Write-Host ""
        Write-Host "Installed, but these commands are missing: $($missingCommands -join ', ')"
        Write-Host "Close the mcc-claude window(s) and re-run the install command."
        exit 1
    }

    Write-ShadowingProgramWarning -Shadowed $shadowedCommands -BinDir $toolBinPath

    $installedVersion = Invoke-NativeCapture -FilePath $installedCommands["mcc-server"] -Arguments @("--version")
    if ($installedVersion -ne "my-claude-code $ExpectedVersion") {
        throw "Expected my-claude-code $ExpectedVersion; found: $installedVersion"
    }
}

function Write-MccCommandReference {
    # Shown after a successful install (direct or deferred) so the user sees the
    # same command reference on Windows as on Linux/WSL.
    Write-Host ""
    Write-Host "Start the proxy:"
    Write-Host "  mcc-server              Start the local proxy and admin dashboard"
    Write-Host ""
    Write-Host "Use a coding agent through the proxy:"
    Write-Host "  mcc-claude              Launch Claude Code through the proxy"
    Write-Host "  mcc-claude --discover-models   Enable the model picker from the catalog"
    Write-Host "  mcc-codex               Launch Codex through the proxy"
    Write-Host "  mcc-pi                  Launch Pi through the proxy"
    Write-Host "  mcc-opencode            Launch OpenCode through the proxy"
    Write-Host "  mcc-opencode2           Launch the OpenCode 2 preview through the proxy"
    Write-Host "  mcc-kilo                Launch Kilo CLI through the proxy"
    Write-Host "  mcc-commandcode         Launch Command Code through the proxy"
    Write-Host "  mcc-kimi                Launch Kimi Code through the proxy"
    Write-Host "  mcc-qwen                Launch Qwen Code through the proxy"
    Write-Host "  mcc-crush               Launch Crush through the proxy"
    Write-Host "  mcc-cline               Launch Cline through the proxy"
    Write-Host "  mcc-goose               Launch Goose through the proxy"
    Write-Host "  mcc-aider               Launch Aider through the proxy"
    Write-Host "  mcc-droid               Launch Droid through the proxy"
    Write-Host "  mcc-gemini              Launch Gemini CLI through the proxy"
    Write-Host "  mcc-desktop             Open the system tray app (desktop)"
    Write-Host ""
    Write-Host "Manage and inspect:"
    Write-Host "  mcc-init                Create or repair ~/.mcc/.env"
    Write-Host "  mcc-rtk                 Manage the RTK token optimizer"
    Write-Host "  mcc-apps                Point desktop apps here (list/status/configure/undo)"
    Write-Host "  mcc-help                Show what each command does"
    if ($script:EnableDesktop) {
        Write-Host ""
        if ($script:DesktopShortcutPath) {
            Write-Host "Start Menu shortcut: $($script:DesktopShortcutPath)"
        }
        elseif ($script:DesktopShortcutError) {
            Write-Host "The Start Menu shortcut was not created: $($script:DesktopShortcutError)"
            Write-Host "Run mcc-desktop directly, or rerun this installer with -Desktop."
        }
    }
    Write-Host ""
    Write-Host "The legacy fcc-* commands (fcc-server, fcc-claude, ...) remain as aliases."
    Write-Host ""
    Write-Host "If mcc-server is not found, open a new terminal: this install may have added"
    Write-Host "a directory to PATH that shells started earlier cannot see."
    Write-Host ""
    Write-Host "To use an update installed while the server is running, restart the proxy"
    Write-Host "with: mcc-server"
}

function Get-LauncherInBinDirectory {
    param(
        [Parameter(Mandatory = $true)] [string] $BinDir,
        [Parameter(Mandatory = $true)] [string] $Name
    )

    # uv writes .exe shims on Windows. The other shapes are here so this
    # answers the same question about any launcher a bin directory can hold,
    # which is also what lets the installer tests drive it with .cmd stubs.
    foreach ($suffix in @(".exe", ".cmd", ".bat", "")) {
        $candidate = Join-Path $BinDir ($Name + $suffix)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Get-NpmGlobalBinDirectory {
    # Where npm writes the shims for a global install. On Windows the prefix IS the
    # directory holding them (%APPDATA%\npm\<name>.cmd), not a bin/ under it,
    # and npm.cmd itself lives there too. No subprocess: this runs once per
    # shadowed name and only to make a warning more useful.
    $directories = @()
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        $directories += (Join-Path $env:APPDATA "npm")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:npm_config_prefix)) {
        $directories += $env:npm_config_prefix
    }
    $npm = Get-ApplicationCommand "npm"
    if ($npm) {
        $directories += (Split-Path -Parent $npm.Source)
    }
    return @($directories | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Get-ShadowingProgram {
    param(
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [string] $BinDir
    )

    # A diagnosis, never a verification: every path out of here that is not "a
    # different file answers to this name" returns $null rather than failing.
    try {
        $command = Get-ApplicationCommand $Name
        if (-not $command) {
            return $null
        }
        $directory = ([IO.Path]::GetFullPath((Split-Path -Parent $command.Source))).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
        if ($directory.Equals($BinDir, [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
        return $command.Source
    }
    catch {
        return $null
    }
}

function Write-ShadowingProgramWarning {
    param(
        [object[]] $Shadowed = @(),
        [string] $BinDir = ""
    )

    if ($null -eq $Shadowed -or $Shadowed.Count -eq 0) {
        return
    }

    $npmDirectories = @()
    foreach ($directory in (Get-NpmGlobalBinDirectory)) {
        try {
            $npmDirectories += ([IO.Path]::GetFullPath($directory)).TrimEnd(
                [IO.Path]::DirectorySeparatorChar,
                [IO.Path]::AltDirectorySeparatorChar
            )
        }
        catch {
            # An unusable prefix simply cannot be the one shadowing us.
        }
    }

    $fromNpm = $false
    Write-Host ""
    foreach ($entry in $Shadowed) {
        Write-Host "WARNING: Another program earlier on PATH answers to this name: $($entry.Name) -> $($entry.Path)"
        foreach ($directory in $npmDirectories) {
            if ($entry.Path.StartsWith($directory + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                $fromNpm = $true
            }
        }
    }
    Write-Host "The install itself is fine: every command was verified in $BinDir."
    Write-Host "Until that other program is removed, or the uv bin directory comes first on PATH, typing the name above runs it instead."
    if ($fromNpm) {
        Write-Host "That path belongs to npm. An older version of the npm package published this name; remove it with:"
        Write-Host "  npm uninstall -g @firedmosquito831/my-claude-code"
    }
}

function Get-LauncherCommands {
    # Both command families share the tool bin directory, so any of them holds
    # the shim uv must replace.
    #
    # This MUST list every name in [project.scripts] and [project.gui-scripts].
    # It silently fell four features behind -- mcc-desktop, mcc-rtk, mcc-help
    # and mcc-anthropic-oauth-login -- and a running mcc-desktop was therefore
    # invisible here. uv then tried to delete a tool environment whose
    # pythonw.exe was still live, failed with "Access is denied (os error 5)",
    # and left the install half-removed. A contract test now compares this list
    # against pyproject so it cannot drift again.
    return @(
        "fcc-server", "fcc-claude", "fcc-claude-old", "fcc-codex", "fcc-pi",
        "fcc-init", "fcc-chatgpt-oauth-login", "fcc-compact-log",
        "free-claude-code",
        "fcc-anthropic-oauth-login", "fcc-rtk", "fcc-help", "fcc-desktop",
        "mcc-server", "mcc-claude", "mcc-claude-old", "mcc-codex", "mcc-pi",
        "mcc-opencode", "mcc-opencode2", "mcc-kilo", "mcc-commandcode", "mcc-kimi",
        "mcc-qwen", "mcc-crush",
        "mcc-cline", "mcc-goose", "mcc-aider", "mcc-droid", "mcc-gemini",
        "mcc-init", "mcc-chatgpt-oauth-login", "mcc-compact-log",
        "mcc-anthropic-oauth-login", "mcc-rtk", "mcc-help", "mcc-migrate",
        "mcc-apps", "mcc-desktop", "my-claude-code",
        "fcc-migrate"
    )
}

function Get-McmHolders {
    # Name the My Claude Code processes running out of one of $Roots -- the
    # launcher shims and the tool environment interpreter this install is
    # about to replace.
    #
    # A locked launcher or a locked tool environment on Windows always has a
    # holder, and until 6.72.2 the install told the user only that something
    # was "in use". On the machine this was written for, the holders were two
    # mcc-server launches from the previous day that were still serving work --
    # and the install printed nothing about them, so the retries looked like
    # bad luck rather than a consequence with a name.
    #
    # This NEVER stops anything. The server it names may be one the user is
    # actively using; the install's answer to a locked file stays what 6.33.1
    # made it, which is to stage the new shims beside and place what it can.
    #
    # Matched on the resolved ExecutablePath being under one of the roots we
    # are about to write to -- never on an image name, because every MCC
    # command on Windows is called python.exe and two of them belong to the
    # user's own coding agents.
    param([string[]]$Roots, [string]$ToolRoot = "")

    $normalised = @()
    foreach ($root in $Roots) {
        if ($root) { $normalised += ($root.TrimEnd('\', '/') + '\') }
    }
    if ($normalised.Count -eq 0) { return }
    # NOT $toolRoot: PowerShell variable names are case-insensitive, so a local
    # $toolRoot IS the $ToolRoot parameter and assigning "" to it here silently
    # emptied the argument the caller passed. An empty prefix then made
    # StartsWith("") true for every path, and the report named the user's own
    # claude.exe as a holder of files this install does not touch.
    $toolRootPrefix = ""
    if ($ToolRoot) { $toolRootPrefix = $ToolRoot.TrimEnd('\', '/') + '\' }

    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        $exe = $process.ExecutablePath
        if (-not $exe) { continue }
        $underRoot = $false
        foreach ($root in $normalised) {
            if ($exe.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                $underRoot = $true
                break
            }
        }
        if (-not $underRoot) { continue }
        # Under a root is not enough. The bin directory holds other programs'
        # shims too -- Claude Code's own claude.exe sits right beside
        # mcc-claude.exe -- and this install replaces none of them, so naming
        # them would be a false accusation in the one message a user reads
        # while an install is retrying. Keep only executables that are ours:
        # the uv tool environment's interpreter, or a launcher from the
        # mcc-/fcc- command family.
        $leaf = [IO.Path]::GetFileNameWithoutExtension($exe).ToLowerInvariant()
        $isOurs = $false
        foreach ($prefix in @('mcc-', 'fcc-', 'my-claude-code', 'free-claude-code')) {
            if ($leaf.StartsWith($prefix)) { $isOurs = $true; break }
        }
        if (-not $isOurs -and $toolRootPrefix -and $exe.StartsWith($toolRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $isOurs = $true
        }
        if (-not $isOurs) { continue }
        $started = ''
        if ($process.CreationDate) {
            $started = $process.CreationDate.ToString('yyyy-MM-dd HH:mm:ss')
        }
        [pscustomobject]@{
            ProcessId = $process.ProcessId
            Name      = $process.Name
            Path      = $exe
            Started   = $started
        }
    }
}

function Write-McmHolderReport {
    # One block naming who is holding the files this install wants to replace,
    # and one sentence saying that nothing will be stopped.
    param([string[]]$Roots, [string]$ToolRoot = "")

    $holders = @(Get-McmHolders -Roots $Roots -ToolRoot $ToolRoot)
    if ($holders.Count -eq 0) {
        Write-Host "No running My Claude Code process is using those files; the lock is something else (an antivirus scan, or Explorer reading an icon)."
        return
    }
    Write-Host "These My Claude Code processes are running from the files being replaced:"
    foreach ($holder in $holders) {
        Write-Host "  pid $($holder.ProcessId)  $($holder.Name)  started $($holder.Started)  $($holder.Path)"
    }
    Write-Host "Nothing above will be stopped: one of them may be a server you are using right now. The new version is being placed beside them instead, and they keep running the version they started with."
}

function Get-RunningLaunchers {
    # Return the process objects of any launcher currently running. These are the
    # processes whose shims uv must replace, so an install cannot proceed while
    # they live. We defer rather than refuse: the update completes after the app
    # is restarted, exactly as on POSIX.
    $running = @()
    foreach ($commandName in Get-LauncherCommands) {
        $processes = @(Get-Process -Name $commandName -ErrorAction SilentlyContinue)
        foreach ($process in $processes) {
            $running += $process
        }
    }
    # Emit to the pipeline (no explicit return) so the caller's @(...) captures
    # 0, 1, or many results as an array. A bare return of $running would unwrap
    # a single Process object into a scalar and a later `.Count` would fail
    # under Set-StrictMode.
    foreach ($process in $running) {
        $process
    }
}

function Start-DeferredInstall {
    param(
        [Parameter(Mandatory = $true)] [string] $UvPath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments,
        [Parameter(Mandatory = $true)] [string] $WheelPath,
        [Parameter(Mandatory = $true)] [object[]] $Running,
        [Parameter(Mandatory = $true)] [string] $Version
    )

    # Keep the verified wheel where a detached helper can reach it. The wheel
    # directory must survive this process exiting, so stage under TEMP, not a
    # tempfile that is deleted on scope exit.
    $stageDir = Join-Path ([IO.Path]::GetTempPath()) ("mcc-deferred-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stageDir | Out-Null
    $stagedWheel = Join-Path $stageDir (Split-Path -Leaf $WheelPath)
    Copy-Item -LiteralPath $WheelPath -Destination $stagedWheel -Force
    Remove-Item -LiteralPath (Split-Path -Parent $WheelPath) -Recurse -Force -ErrorAction SilentlyContinue

    # The last argument is the package spec ("my-claude-code[...] @ file:///...").
    # Point it at the staged wheel so the detached helper installs the verified
    # artifact, not the temp copy we just deleted. Preserve any extras prefix
    # (voice / voice_local) by reusing the part before " @ ".
    $stagedUrl = ([Uri]::new($stagedWheel)).AbsoluteUri
    if ($Arguments.Count -gt 0) {
        $last = $Arguments[-1]
        $packagePrefix = ($last -split " @ ", 2)[0]
        $stagedSpec = "$packagePrefix @ $stagedUrl"
        $Arguments = $Arguments[0..($Arguments.Count - 2)] + $stagedSpec
    }

    # Wait for every running launcher to exit (bounded), then install the staged
    # wheel. The user restarts the app themselves; we must NOT start it, or we
    # would replace the same processes we waited for. Retry the install with
    # backoff because handle release is not instantaneous on Windows.
    #
    # Every launcher is waited on, including the tray: this path now runs only
    # when the TOOL DIRECTORY rename was refused, and every launcher process --
    # tray included -- runs its interpreter out of that directory, so every one
    # of them holds it. (Shim locks no longer reach here; they are handled by
    # renaming the shims aside.)
    #
    # Pin each process's identity with its creation time, not the id alone.
    # Windows recycles process ids quickly, so a bare `Get-Process -Id` can
    # match an unrelated process that inherited the id and wait out the whole
    # deadline without ever installing. Same id but a different start time means
    # the launcher is gone. A start time we cannot read is recorded as 0 and
    # falls back to the id alone, which is the previous behaviour rather than a
    # new failure mode -- never treat "unknown" as "gone", or we would install
    # underneath a process that has not exited yet. Mirrors the helper in
    # release_updates.py.
    $pidsLiteral = ($Running | ForEach-Object {
        $startTime = 0
        try { $startTime = $_.StartTime.ToFileTimeUtc() } catch { $startTime = 0 }
        "@{ Id = " + $_.Id.ToString() + "; Start = " + $startTime.ToString() + " }"
    }) -join ", "
    # The detached helper must invoke uv as a real command. The uv path and the
    # argument array are emitted as literal PowerShell values ($uvPath /
    # $installArgs) so the helper calls `& $uvPath @installArgs`. Two ways to
    # get this wrong, both of which silently install nothing:
    #   * a command built as a single string at statement position is treated as
    #     a command NAME and never executed;
    #   * `@$installArgs` is NOT splatting. Splatting is `@installArgs` -- the
    #     sigil replaces the `$`. `@$installArgs` evaluates `$installArgs` and
    #     array-subexpressions it, so the whole argument array collapses into a
    #     single argument and uv reports an unknown command.
    $uvPathLiteral = "'" + ($UvPath -replace "'", "''") + "'"
    $installArgsLiteral = "@(" + (($Arguments | ForEach-Object {
        "'" + ($_ -replace "'", "''") + "'"
    }) -join ", ") + ")"
    $stageDirLiteral = "'" + ($stageDir -replace "'", "''") + "'"
    $DeferredWaitSeconds = 600
    $DeferredWaitMinutes = [int]($DeferredWaitSeconds / 60)

    $script = @"
`$ErrorActionPreference = 'Stop'
# Ten minutes, not six hours. The old deadline meant a helper could sit
# resident for the rest of the day behind a launcher that was never going to
# exit -- and then not install anyway. Ten minutes is long enough for a user
# who has just been told "stop the running app" to do so, and short enough that
# a forgotten helper is a nuisance rather than a resident process. Past it the
# helper escalates to the EXACT pids recorded below, whose identity is pinned
# by creation time, and then installs. Mirrors the bound the server itself and
# the dashboard updater's helper now apply to a stop.
`$deadline = (Get-Date).AddSeconds($DeferredWaitSeconds)
`$targets = @($pidsLiteral)
function Test-TargetAlive {
    param([hashtable] `$Target)
    `$proc = Get-Process -Id `$Target.Id -ErrorAction SilentlyContinue
    if (-not `$proc) { return `$false }
    # 0 means we could not read the launcher's start time; fall back to the id
    # alone rather than risk installing underneath a live launcher.
    if (`$Target.Start -eq 0) { return `$true }
    try { return `$proc.StartTime.ToFileTimeUtc() -eq `$Target.Start }
    catch { return `$false }
}
while ((Get-Date) -lt `$deadline) {
    `$alive = @(`$targets | Where-Object { Test-TargetAlive -Target `$_ })
    if (`$alive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
}
`$stuck = @(`$targets | Where-Object { Test-TargetAlive -Target `$_ })
if (`$stuck.Count -gt 0) {
    Write-Host "My Claude Code did not stop within $DeferredWaitMinutes minutes; stopping it to apply the update."
    foreach (`$target in `$stuck) {
        Stop-Process -Id `$target.Id -Force -ErrorAction SilentlyContinue
    }
    `$killDeadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt `$killDeadline) {
        if (@(`$targets | Where-Object { Test-TargetAlive -Target `$_ }).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
}
if (@(`$targets | Where-Object { Test-TargetAlive -Target `$_ }).Count -gt 0) {
    Write-Host "My Claude Code could not be stopped; install not applied."
    exit 1
}
Start-Sleep -Seconds 2
`$uvPath = $uvPathLiteral
`$installArgs = $installArgsLiteral
`$ErrorActionPreference = 'Continue'
`$delays = @(0, 5, 10, 20, 30)
`$ok = `$false
foreach (`$wait in `$delays) {
    if (`$wait -gt 0) { Start-Sleep -Seconds `$wait }
    & `$uvPath @installArgs 2>&1 | Out-String | Out-Null
    if (`$LASTEXITCODE -eq 0) { `$ok = `$true; break }
}
`$ErrorActionPreference = 'Stop'
if (`$ok) {
    Remove-Item -Path $stageDirLiteral -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "My Claude Code install completed. Start the app with: mcc-server"
}
else {
    # Sweep the staged wheel on the failing branch too. Leaving it behind was
    # how abandoned stage directories accumulated under TEMP; the user re-runs
    # the installer, which downloads and verifies a fresh wheel anyway. The
    # helper script itself is held open by this very process, so only the wheel
    # actually goes -- best-effort, exactly as on the success branch.
    Remove-Item -Path $stageDirLiteral -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "My Claude Code install failed after multiple attempts. Re-run the installer."
}
"@

    $helperPath = Join-Path $stageDir "apply-update.ps1"
    Set-Content -LiteralPath $helperPath -Value $script -Encoding UTF8

    # Start-Process has no creation-flags parameter, so the flags this comment
    # used to compute were never applied to anything. Ask for the window state
    # we can actually get and drop the dead value rather than keep a constant
    # that documents a behaviour the child does not have.
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $helperPath) `
        -WindowStyle Hidden `
        -PassThru
    $null = $process

    $script:Deferred = $true

    Write-Host "My Claude Code is currently running. The update to v$Version is staged and"
    Write-Host "will complete after you stop the running app, then restart it (mcc-server)."
    Write-Host "The new version is picked up on restart."
    Write-MccCommandReference
    return $Version
}

if ($Help) {
    Show-Usage
    return
}

if ($RemainingArgs.Count -gt 0) {
    Show-Usage
    throw "Unknown option: $($RemainingArgs -join ' ')"
}

if ((-not [string]::IsNullOrWhiteSpace($TorchBackend)) -and (-not ($VoiceLocal -or $VoiceAll))) {
    throw "-TorchBackend requires -VoiceLocal or -VoiceAll."
}

Add-KnownBinDirectories

# ONE update at a time, whichever path started it (decision Q5). A second
# installer does not queue and does not install: it names the owner, points at
# the transcript that owner is writing, and exits 0.
if (-not (Enter-UpdateLock)) {
    Write-WatchingInsteadNotice -Owner $script:UpdateLockOwner
    return
}

# Everything from here to the end runs under the lock. The body below keeps its
# original indentation deliberately: this `try` exists only so the lock is
# released on every path out, a throw from any step included, and reindenting
# three hundred lines to say so would bury the change that matters under
# whitespace.
try {

Write-Step "Ensuring uv $MinUvVersion or newer is installed"
Ensure-Uv

Write-Step "Installing Python $PythonVersion through uv"
Install-ManagedPython

Write-Step "Installing or updating My Claude Code"
# From here until the last line of this script, anything that reads the update
# receipt sees an installer in flight and stays out of the way. `finally` is
# load-bearing: a throw between here and the end would otherwise leave
# `installing` on disk with no terminal record, and the pid check would keep it
# believed for as long as this pid stays alive.
Write-InstallProgress -Stage 'installing' -Message 'Installing the new version.'
try {
    $InstalledVersion = Install-FreeClaudeCode
}
catch {
    $script:InstallProgressRestarted = $false
    Write-InstallProgress -Stage 'failed' -Message 'The install failed.'
    throw
}
$script:InstallProgressVersion = $InstalledVersion

if ($script:RenamedWhileRunning) {
    # Installed while launchers were open: the old tool env and the old shims
    # were renamed aside and uv wrote a complete fresh set. Verification below
    # is the same full check as any other install -- it is not relaxed because
    # something was running.
    Write-Step "Configuring PATH and verifying My Claude Code"
    # The timeline's fourth stage. A hand-run install narrates itself in the
    # same vocabulary the deferred helper uses, so a window watching this file
    # shows the same sequence whichever installer is running.
    Write-InstallProgress -Stage 'verifying' -Message 'Checking that every command is in place.'
    Configure-AndConfirmFreeClaudeCode -ExpectedVersion $InstalledVersion

    Invoke-PrecompileBytecode -UvPath (Resolve-UvPath -Purpose "precompiling")
    Enable-RtkForAgents
    New-DesktopShortcut

    Write-Host ""
    Write-Host "My Claude Code $InstalledVersion is installed and verified."
    Write-Host "New sessions and restarted servers use the new version."
    Write-Host "Already-open windows keep running the previous version until they are"
    Write-Host "closed or restarted."
    if ($script:ShimsKeptInPlace.Count -gt 0) {
        # Not a failure and not a warning to act on: the launcher is a stub that
        # runs the interpreter in the canonical tool directory, which now holds
        # the new install, so these commands already run the new code.
        Write-Host ""
        Write-Host "These launchers were locked and kept the file they had:"
        Write-Host "  $($script:ShimsKeptInPlace -join ', ')"
        Write-Host "These keep working and will refresh on the next install."
    }
    Write-MccCommandReference
}
elseif ($script:Deferred) {
    Write-Host ""
    Write-Host "Update staged for after restart."
    if ($script:EnableDesktop) {
        # The install has not run yet, so mcc-desktop.exe is not in place to
        # export its icon. Say so rather than leaving -Desktop silently ignored.
        Write-Host ""
        Write-Host "The Start Menu shortcut was not created: the install completes after you stop"
        Write-Host "the running app. Rerun this installer with -Desktop once it has finished."
    }
}
elseif ($DryRun) {
    Enable-RtkForAgents
    New-DesktopShortcut

    Write-Host ""
    Write-Host "Dry run complete. No changes were made."
}
else {
    Write-Step "Configuring PATH and verifying My Claude Code"
    Write-InstallProgress -Stage 'verifying' -Message 'Checking that every command is in place.'
    Configure-AndConfirmFreeClaudeCode -ExpectedVersion $InstalledVersion

    Invoke-PrecompileBytecode -UvPath (Resolve-UvPath -Purpose "precompiling")
    Enable-RtkForAgents
    New-DesktopShortcut

    Write-Host ""
    Write-Host "My Claude Code $InstalledVersion is installed and verified."
    Write-MccCommandReference
}

# The terminal record. Every branch above ends here, so whichever way the
# install went, the receipt stops saying "installing" and the helper-alive gate
# reopens for everyone else.
#
# With -Restart the terminal record is the RESTART's: `done` with
# `restarted: true` once a listener answers /health on the configured port, and
# `failed` with the child's exit code and the last lines it wrote when it never
# does. "The install exited 0" is not success -- on 2026-09-11 two installs
# exited 0 fifteen minutes apart with the user's server down through both.
if ($script:NoStartRequested) {
    Write-Host ""
    if ($script:RestartRequested) {
        Write-Host "No server was started: -NoStart (or MCC_INSTALL_NO_START=1) overrides -Restart."
    }
    else {
        Write-Host "No server was started. Start one with: mcc-server"
    }
    Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'
}
elseif ($script:RestartRequested -and (-not $DryRun) -and (-not $script:Deferred)) {
    $null = Invoke-RestartAfterInstall -InstalledVersion $InstalledVersion
}
else {
    Write-InstallProgress -Stage 'done' -Message 'The new version is installed.'
}

}
finally {
    Exit-UpdateLock
}
