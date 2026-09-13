@echo off
rem My Claude Code installer for cmd.exe.
rem
rem Why this file exists: the published Windows route is PowerShell, and
rem PowerShell's execution policy applies to script FILES. The `irm ... | iex`
rem one-liner is fine -- policy does not apply to text executed in the current
rem session -- but the moment someone SAVES install.ps1 and runs it, the default
rem RemoteSigned/Restricted policy refuses it, and the error it prints reads
rem like the product is broken. This batch file is the route with no PowerShell
rem question in it at all: double-click it, or run it from cmd.exe, and it
rem passes -ExecutionPolicy Bypass -File itself.
rem
rem It is deliberately not a second installer. It downloads scripts/install.ps1
rem -- the same file the PowerShell one-liner runs, the one that verifies the
rem release wheel's SHA-256 and provisions uv and Python -- and runs it. Every
rem decision about what to install stays in one script.
rem
rem   install.cmd                     server only
rem   install.cmd --desktop           server plus the Start Menu shortcut
rem   install.cmd --version 6.63.0    pin a release
rem   install.cmd                     install, then restart the server on the
rem                                   port this configuration directory is for
rem                                   and open the desktop app -- the DEFAULT
rem                                   since 7.1.0
rem   install.cmd --restart           accepted and ignored; it is the default
rem   install.cmd --no-restart        install, leave a running server alone
rem   install.cmd --no-start          install and start nothing
rem   install.cmd --no-desktop        install and restart, but no desktop app
rem   install.cmd --dry-run           print what it would do, change nothing
rem
rem   set MCC_INSTALL_REF=my-branch    fetch install.ps1 from that branch, tag
rem                                    or commit instead of main. This is how a
rem                                    change to the installer is proven before
rem                                    it is merged.
rem
rem Notes for anyone editing this file:
rem   * curl.exe ships with Windows 10 1803 and later, and with Windows 11.
rem     It is the one prerequisite this route adds over the PowerShell one
rem     (install.ps1 itself uses Invoke-RestMethod and needs no curl), so its
rem     absence gets one clear sentence, the way install.sh names the curl
rem     package for the local distro.
rem   * every exit is `exit /b <code>` and never a bare `exit`: a bare `exit`
rem     closes the console window when this file is double-clicked, taking the
rem     error message with it.
rem   * the downloaded script is DELETED on success and KEPT on failure, with
rem     its path printed, so a failed install leaves something to read.

setlocal

rem Which revision of install.ps1 to fetch (F1 / decision Q10 PR 3).
rem
rem It used to be the literal string `main`, always, for every run. That meant
rem `install.cmd --version 6.63.0` fetched TODAY's installer and asked it to
rem install a release from last week -- a combination nobody has ever tested --
rem and it meant there was no way at all to prove a branch's installer before
rem merging it, because the only thing this file would ever run was main's.
rem
rem   * --version X            -> the tag vX, so the installer that runs is the
rem                              one that shipped with the release being asked
rem                              for
rem   * MCC_INSTALL_REF=<ref>  -> that branch, tag or commit, which is how a
rem                              change to install.ps1 is proven before it is
rem                              merged. It wins over --version deliberately:
rem                              someone who names a ref is testing that ref.
rem   * neither                -> main, exactly as before
set "MCC_REF=main"
set "MCC_SCRIPT=%TEMP%\install-mcc.ps1"
set "MCC_PSARGS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--desktop" goto arg_desktop
if /i "%~1"=="--rtk" goto arg_rtk
if /i "%~1"=="--restart" goto arg_restart
if /i "%~1"=="--no-restart" goto arg_norestart
if /i "%~1"=="--no-start" goto arg_nostart
if /i "%~1"=="--no-desktop" goto arg_nodesktop
if /i "%~1"=="--dry-run" goto arg_dryrun
if /i "%~1"=="--help" goto arg_help
if /i "%~1"=="-h" goto arg_help
if /i "%~1"=="--voice-nim" goto arg_voicenim
if /i "%~1"=="--voice-local" goto arg_voicelocal
if /i "%~1"=="--voice-all" goto arg_voiceall
if /i "%~1"=="--version" goto arg_version
if /i "%~1"=="--torch-backend" goto arg_torch
echo install.cmd: unknown option "%~1"
echo Run install.cmd --help for the list.
exit /b 2

:arg_desktop
set "MCC_PSARGS=%MCC_PSARGS% -Desktop"
shift
goto parse

:arg_rtk
set "MCC_PSARGS=%MCC_PSARGS% -Rtk"
shift
goto parse

:arg_dryrun
set "MCC_PSARGS=%MCC_PSARGS% -DryRun"
shift
goto parse

rem D5 shape kept: this file decides nothing about the install. Every one of
rem these is passed straight through to install.ps1, which owns what a restart
rem means -- one server, the one on the port of the configuration directory
rem this install is for. Passing NOTHING is the default, and since 7.1.0 the
rem default is to restart that server and open the desktop app.
:arg_restart
set "MCC_PSARGS=%MCC_PSARGS% -Restart"
shift
goto parse

:arg_norestart
set "MCC_PSARGS=%MCC_PSARGS% -NoRestart"
shift
goto parse

:arg_nostart
set "MCC_PSARGS=%MCC_PSARGS% -NoStart"
shift
goto parse

:arg_nodesktop
set "MCC_PSARGS=%MCC_PSARGS% -NoDesktop"
shift
goto parse

:arg_help
set "MCC_PSARGS=%MCC_PSARGS% -Help"
shift
goto parse

:arg_voicenim
set "MCC_PSARGS=%MCC_PSARGS% -VoiceNim"
shift
goto parse

:arg_voicelocal
set "MCC_PSARGS=%MCC_PSARGS% -VoiceLocal"
shift
goto parse

:arg_voiceall
set "MCC_PSARGS=%MCC_PSARGS% -VoiceAll"
shift
goto parse

:arg_version
if "%~2"=="" goto missing_version
set "MCC_PSARGS=%MCC_PSARGS% -Version "%~2""
rem The installer for a release is the one that shipped with it. Tags are
rem `v<version>` and install.ps1 has lived at scripts/install.ps1 for every one
rem of them.
set "MCC_REF=v%~2"
shift
shift
goto parse

:arg_torch
if "%~2"=="" goto missing_torch
set "MCC_PSARGS=%MCC_PSARGS% -TorchBackend "%~2""
shift
shift
goto parse

:missing_version
echo install.cmd: --version needs a release, for example: install.cmd --version 6.63.0
exit /b 2

:missing_torch
echo install.cmd: --torch-backend needs a backend, for example: install.cmd --torch-backend cu130
exit /b 2

:parsed
rem An explicitly named ref wins: whoever set it is proving that ref.
if not "%MCC_INSTALL_REF%"=="" set "MCC_REF=%MCC_INSTALL_REF%"
set "MCC_RAW=https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/%MCC_REF%/scripts/install.ps1"

where curl.exe >nul 2>&1
if errorlevel 1 goto no_curl

echo Downloading the My Claude Code installer (%MCC_REF%)...
curl.exe -fsSL -o "%MCC_SCRIPT%" "%MCC_RAW%"
if errorlevel 1 goto ref_fallback
if not exist "%MCC_SCRIPT%" goto ref_fallback
goto downloaded

rem A pin that does not resolve must not be a dead end. A tag can be missing
rem (a release that does not exist), and a tag old enough can predate
rem scripts/install.ps1 itself. Say so and fall back to main, which is what
rem this file did unconditionally before 6.82.0 -- the pin is an improvement on
rem the default, not a new way to fail.
:ref_fallback
if "%MCC_REF%"=="main" goto download_failed
echo install.cmd: no installer at %MCC_REF%; falling back to main.
set "MCC_REF=main"
set "MCC_RAW=https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts/install.ps1"
curl.exe -fsSL -o "%MCC_SCRIPT%" "%MCC_RAW%"
if errorlevel 1 goto download_failed
if not exist "%MCC_SCRIPT%" goto download_failed

:downloaded

powershell -NoProfile -ExecutionPolicy Bypass -File "%MCC_SCRIPT%"%MCC_PSARGS%
set "MCC_EXIT=%ERRORLEVEL%"
if not "%MCC_EXIT%"=="0" goto install_failed
del /q "%MCC_SCRIPT%" >nul 2>&1
exit /b 0

:install_failed
echo.
echo The install did not finish (exit code %MCC_EXIT%).
echo The installer script was kept so you can read it: %MCC_SCRIPT%
exit /b %MCC_EXIT%

:download_failed
echo.
echo install.cmd: could not download the installer from %MCC_RAW%
echo Check your network connection, or set HTTPS_PROXY if you are behind a proxy.
exit /b 1

:no_curl
echo.
echo install.cmd: curl.exe was not found on PATH.
echo curl.exe ships with Windows 10 1803 and later and with Windows 11; on an
echo older machine, use the PowerShell route from the README instead:
echo.
echo   https://github.com/FiredMosquito831/my-claude-code#install
echo.
exit /b 1
