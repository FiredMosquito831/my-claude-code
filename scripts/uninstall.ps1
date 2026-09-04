param(
    [switch] $DryRun,
    [switch] $Help,
    [Parameter(ValueFromRemainingArguments = $true)]
    [object[]] $RemainingArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# PackageName must equal [project].name in pyproject.toml -- uv installs and
# uninstalls by that name, and anything else silently no-ops. Both names are
# pinned by tests/contracts/test_uninstaller_parity.py.
$PackageName = "my-claude-code"
# Installs older than 5.14 were published under the free-claude-code name;
# kept as best-effort cleanup. Absence of either tool is acceptable.
$LegacyPackageName = "free-claude-code"
$FccHomeDirname = ".fcc"
# Must mirror every entry in [project.scripts] + [project.gui-scripts] (the
# same list as Get-LauncherCommands in scripts/install.ps1); pinned by
# tests/contracts/test_uninstaller_parity.py.
$FccCommands = @(
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
    "mcc-desktop", "my-claude-code",
    "fcc-migrate"
)
# GUI scripts (mcc-desktop / fcc-desktop) run as pythonw.exe out of the uv
# tool environment rather than under a shim named after the command, so the
# process guard must also look at interpreter image names.
$GuardProcessImages = @("pythonw")
# Desktop integration artefacts. These live OUTSIDE the config directory, so
# purging ~/.mcc and ~/.fcc never reaches them and an uninstall used to leave
# a Start Menu entry pointing at a deleted shim plus an autostart value that
# relaunched a package that is no longer installed.
#   * the shortcut is written by New-DesktopShortcut in scripts/install.ps1
#     (install.ps1 -Desktop) under %APPDATA%;
#   * the HKCU Run value is written by _apply_windows_start_at_login in
#     src/my_claude_code/config/desktop.py (WINDOWS_RUN_VALUE).
# The exported icon (~/.mcc/app-icon.ico) is inside the config directory and
# is removed by Purge-FccHome. Every pairing here is pinned by
# tests/contracts/test_uninstaller_parity.py.
$StartMenuRelativeDir = "Microsoft\Windows\Start Menu\Programs"
$StartMenuShortcutName = "My Claude Code.lnk"
$WindowsRunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$WindowsRunValueName = "MyClaudeCodeDesktop"
$script:UvPath = ""
$script:UvToolBin = ""

function Show-Usage {
    @"
Usage: uninstall.ps1 [options]

Removes the My Claude Code uv tool (plus any legacy Free Claude Code tool),
the Start Menu shortcut and the start-at-login registration, and deletes
~/.mcc/ and ~/.fcc/ after removal is verified.
Does not remove uv, Claude Code, Codex, Pi, the uv-managed Python runtime, shared
PATH entries, or ~/.fcc-old (which holds the rollback note).

Options:
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

function Get-ApplicationCommand {
    param([string] $Name)

    $commands = @(Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
    if ($commands.Count -eq 0) {
        return $null
    }
    return $commands[0]
}

function Invoke-NativeResult {
    param(
        [string] $FilePath,
        [string[]] $Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $global:LASTEXITCODE = 0
        $output = (& $FilePath @Arguments 2>&1 | Out-String).Trim()
        return [pscustomobject] @{
            ExitCode = $LASTEXITCODE
            Output = $output
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Test-MissingUvToolError {
    param(
        [string] $ToolName,
        [string] $Output
    )

    $normalized = $Output.ToLowerInvariant()
    return $normalized.Contains($ToolName) -and $normalized.Contains("is not installed")
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

function Add-KnownUvPaths {
    Add-PathEntry (Join-Path $env:USERPROFILE ".local\bin")
    Add-PathEntry (Join-Path $env:USERPROFILE ".cargo\bin")
}

function Assert-NoMccProcessesRunning {
    # Shim-name checks cannot see gui-script processes (pythonw.exe out of the
    # tool environment), hence the extra interpreter-image list.
    $running = @()
    foreach ($commandName in ($FccCommands + $GuardProcessImages)) {
        $processes = @(Get-Process -Name $commandName -ErrorAction SilentlyContinue)
        if ($processes.Count -gt 0) {
            $running += $commandName
        }
    }
    if ($running.Count -gt 0) {
        throw "My Claude Code is still running ($($running -join ', ')). Stop those processes, then rerun uninstall."
    }
}

function Initialize-UvContext {
    Add-KnownUvPaths

    if ($DryRun) {
        Write-Host "+ uv tool dir --bin"
        return
    }

    $uvCommand = Get-ApplicationCommand "uv"
    if (-not $uvCommand) {
        throw "uv is required to remove the My Claude Code tool. Install uv, then rerun this uninstaller; ~/.fcc was not deleted."
    }
    $script:UvPath = $uvCommand.Source

    $commandText = Format-Command -FilePath $script:UvPath -Arguments @("tool", "dir", "--bin")
    Write-Host "+ $commandText"
    $result = Invoke-NativeResult -FilePath $script:UvPath -Arguments @("tool", "dir", "--bin")
    if ($result.ExitCode -ne 0) {
        if (-not [string]::IsNullOrWhiteSpace($result.Output)) {
            [Console]::Error.WriteLine($result.Output)
        }
        throw "Could not determine the uv tool bin directory (exit code $($result.ExitCode)); ~/.fcc was not deleted."
    }
    $script:UvToolBin = $result.Output.Trim()
    if ([string]::IsNullOrWhiteSpace($script:UvToolBin)) {
        throw "uv returned an empty tool bin directory; ~/.fcc was not deleted."
    }
}

function Uninstall-MccTool {
    param([string] $ToolName)

    Write-Host "+ uv tool uninstall $ToolName"
    if ($DryRun) {
        return
    }

    $result = Invoke-NativeResult -FilePath $script:UvPath -Arguments @(
        "tool",
        "uninstall",
        $ToolName
    )
    if ($result.ExitCode -eq 0) {
        if (-not [string]::IsNullOrWhiteSpace($result.Output)) {
            Write-Host $result.Output
        }
        return
    }
    if (Test-MissingUvToolError -ToolName $ToolName -Output $result.Output) {
        Write-Host "$ToolName uv tool is already absent; verifying its entry points."
        return
    }
    if (-not [string]::IsNullOrWhiteSpace($result.Output)) {
        [Console]::Error.WriteLine($result.Output)
    }
    throw "uv tool uninstall $ToolName failed with exit code $($result.ExitCode); ~/.fcc was not deleted."
}

function Confirm-FccCommandsRemoved {
    if ($DryRun) {
        Write-Host "+ verify all My Claude Code entry points are absent from the uv tool bin directory"
        return
    }

    $remaining = @()
    $extensions = @("", ".exe", ".cmd", ".bat", ".ps1")
    foreach ($commandName in $FccCommands) {
        foreach ($extension in $extensions) {
            $commandPath = Join-Path $script:UvToolBin "$commandName$extension"
            if (Test-Path -LiteralPath $commandPath) {
                $remaining += $commandPath
            }
        }
    }
    if ($remaining.Count -gt 0) {
        throw "My Claude Code entry points remain after uv uninstall: $($remaining -join ', '); ~/.fcc was not deleted."
    }
}

function Remove-StartMenuShortcut {
    # install.ps1 -Desktop writes "%APPDATA%\...\Start Menu\Programs\My Claude
    # Code.lnk" targeting the mcc-desktop shim. uv tool uninstall deletes the
    # shim; the .lnk survived it and stayed in the Start Menu forever.
    if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
        Write-Host "APPDATA is not set; skipping the Start Menu shortcut."
        return
    }

    $startMenuDir = Join-Path $env:APPDATA $StartMenuRelativeDir
    $shortcutPath = Join-Path $startMenuDir $StartMenuShortcutName
    if (-not (Test-Path -LiteralPath $shortcutPath)) {
        Write-Host "No Start Menu shortcut to remove: $shortcutPath"
        return
    }

    $commandText = @(
        "Remove-Item",
        "-LiteralPath",
        (Format-Argument $shortcutPath),
        "-Force"
    ) -join " "
    Write-Host "+ $commandText"
    if ($DryRun) {
        return
    }

    try {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    catch {
        # A shortcut the shell has open is annoying, not fatal: the tool and
        # the config are already gone, so refusing here would strand the user
        # mid-uninstall with nothing left to retry.
        Write-Warning "Could not remove the Start Menu shortcut ($shortcutPath): $($_.Exception.Message)"
        return
    }
    if (Test-Path -LiteralPath $shortcutPath) {
        Write-Warning "The Start Menu shortcut still exists after deletion: $shortcutPath"
        return
    }
    Write-Host "Removed Start Menu shortcut: $shortcutPath"
}

function Remove-StartAtLoginRegistration {
    # mcc-desktop --start-at-login writes the HKCU Run value named by
    # WINDOWS_RUN_VALUE in src/my_claude_code/config/desktop.py. Left behind,
    # Windows tries to launch an uninstalled package at every login.
    $valueDescription = "$WindowsRunKeyPath\$WindowsRunValueName"
    $existing = $null
    try {
        $existing = Get-ItemProperty -LiteralPath $WindowsRunKeyPath -Name $WindowsRunValueName -ErrorAction Stop
    }
    catch {
        $existing = $null
    }
    if ($null -eq $existing) {
        Write-Host "No start-at-login registration to remove: $valueDescription"
        return
    }

    $commandText = @(
        "Remove-ItemProperty",
        "-LiteralPath",
        (Format-Argument $WindowsRunKeyPath),
        "-Name",
        (Format-Argument $WindowsRunValueName)
    ) -join " "
    Write-Host "+ $commandText"
    if ($DryRun) {
        return
    }

    try {
        Remove-ItemProperty -LiteralPath $WindowsRunKeyPath -Name $WindowsRunValueName -Force
    }
    catch {
        Write-Warning "Could not remove the start-at-login registration ($valueDescription): $($_.Exception.Message)"
        return
    }
    Write-Host "Removed start-at-login registration: $valueDescription"
}

function Remove-DesktopArtifacts {
    Remove-StartMenuShortcut
    Remove-StartAtLoginRegistration
}

function Purge-ConfigDir {
    # Removes one config directory ($dirName) if it exists. The refuse-while-
    # running guard already ran (Assert-NoMccProcessesRunning), so anything still
    # here is safe to delete. $leaveAlone means "report it, but never touch it":
    # that is ~/.fcc-old, which holds the user's rollback note.
    param(
        [Parameter(Mandatory)] [string] $DirName,
        [switch] $LeaveAlone
    )
    # NOTE: the variable is named $targetDir, not $home -- PowerShell
    # variables are case-insensitive and $HOME is a read-only automatic
    # variable, so assigning to $home throws "Cannot overwrite variable
    # HOME".
    $targetDir = Join-Path $env:USERPROFILE $DirName
    if (-not (Test-Path -LiteralPath $targetDir)) {
        return
    }
    if ($LeaveAlone) {
        Write-Host "Leaving $targetDir in place (user data / rollback note)."
        return
    }

    $commandText = @(
        "Remove-Item",
        "-LiteralPath",
        (Format-Argument $targetDir),
        "-Recurse",
        "-Force"
    ) -join " "
    Write-Host "+ $commandText"
    if ($DryRun) {
        return
    }

    Remove-Item -LiteralPath $targetDir -Recurse -Force
    if (Test-Path -LiteralPath $targetDir) {
        throw "$DirName config directory still exists after deletion: $targetDir"
    }
}

function Purge-FccHome {
    # The new default (~/.mcc) and, if still present, the legacy home (~/.fcc).
    # ~/.fcc-old is left untouched: it holds the user's rollback note.
    Purge-ConfigDir -DirName ".mcc"
    Purge-ConfigDir -DirName $FccHomeDirname
    Purge-ConfigDir -DirName ".fcc-old" -LeaveAlone
}

if ($Help) {
    Show-Usage
    return
}
if ($RemainingArgs.Count -gt 0) {
    Show-Usage
    throw "Unknown option: $($RemainingArgs -join ' ')"
}
if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not set; cannot locate My Claude Code data."
}

Write-Step "Checking for running My Claude Code processes"
Assert-NoMccProcessesRunning

Write-Step "Locating the uv-managed My Claude Code installation"
Initialize-UvContext

Write-Step "Removing the My Claude Code uv tool (and any legacy Free Claude Code tool)"
Uninstall-MccTool -ToolName $PackageName
Uninstall-MccTool -ToolName $LegacyPackageName

Write-Step "Verifying My Claude Code entry points were removed"
Confirm-FccCommandsRemoved

Write-Step "Removing the Start Menu shortcut and the start-at-login registration"
Remove-DesktopArtifacts

Write-Step "Purging FCC config and data from ~/.fcc"
Purge-FccHome

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run complete. No changes were made."
}
else {
    Write-Host "My Claude Code has been removed and verified."
    Write-Host "The Start Menu shortcut and the start-at-login registration were removed with it."
    Write-Host "uv, Claude Code, Codex, Pi, the uv-managed Python runtime, and shared PATH entries were left installed."
}
