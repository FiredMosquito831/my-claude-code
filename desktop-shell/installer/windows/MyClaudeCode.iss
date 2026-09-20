; My Claude Code -- Windows desktop-app installer (delivery Path B).
;
; WHAT THIS INSTALLS
;   One file: MyClaudeCode.exe, the desktop shell, plus its icon and a Start
;   Menu shortcut. That is the whole payload.
;
; WHAT THIS DELIBERATELY DOES *NOT* INSTALL
;   Python, uv, the MCC server, or any of the 40 entry points. Decision Q4
;   (spec section 8) moved the bootstrap into the application: when the shell
;   launches and cannot find `mcc-desktop`, it shows the exact install command
;   and runs it itself, streaming the output into the window --
;   `src-tauri/src/install.rs`. An installer that also carried the server
;   would be a second, divergent copy of `scripts/install.ps1`, and would need
;   admin rights the moment it wanted to place a Python. So this installer is
;   ~4 MB, needs no elevation, and the first launch does the rest.
;
;   It also does NOT write the HKCU `Run` value. Start-at-login is the
;   application's own setting: `config/desktop.py` owns that value and
;   `_reconcile_start_at_login` (`cli/desktop.py`) is its only writer. An
;   installer that wrote it would be a second writer of a single-writer value,
;   and uninstalling the desktop app would then silently disable the server
;   tray's autostart. See `tests/contracts/test_uninstaller_parity.py`.
;
; UNINSTALL SPLIT -- read this before adding anything below.
;   "My Claude Code (desktop app)" in Apps & Features removes ONLY what this
;   file created: the exe, the icons, the shortcuts, its own registry key and,
;   if the user says yes, the shell's window-geometry directory. It must never
;   touch `~/.local/bin`, `~/.mcc`, `~/.fcc`, or the HKCU `Run` value.
;   Removing the server is `scripts/uninstall.ps1`'s job, and only on explicit
;   consent.
;
; PRIVILEGES
;   `PrivilegesRequired=lowest` -- a per-user install, no UAC prompt, no
;   SmartScreen-elevation dialog on top of the unsigned-binary one. The
;   uninstall key lands in HKCU\...\Uninstall\{AppId}_is1, which is exactly
;   the shape winget's `Scope: user` + `AppsAndFeaturesEntries.ProductCode`
;   expects (spec S8).
;
; SIGNING
;   None. Decision Q9. See `desktop-shell/README.md` for what SmartScreen
;   shows and why an EV certificate is not the answer.
;
; WHAT THE SHELL NEEDS FROM THE MACHINE, AND WHO PROVIDES IT
;   Two system components, and they fail in completely different ways.
;
;   1. The **Visual C++ runtime**. A Rust MSVC build imports VCRUNTIME140.dll
;      and VCRUNTIME140_1.dll, which are NOT part of Windows -- they come from
;      the Visual C++ redistributable. On a machine without it the *loader*
;      kills the process with 0xC0000135 (STATUS_DLL_NOT_FOUND) before a line
;      of our code, or of Tauri's, runs. That is exactly what
;      microsoft/winget-pkgs PR #430045 hit on its clean validator VM. This
;      installer does not bundle the redistributable (25 MB, and it needs
;      administrator rights, which a `PrivilegesRequired=lowest` install does
;      not have): the shell is built with `-C target-feature=+crt-static`
;      (`../../src-tauri/.cargo/config.toml`), so those imports do not exist.
;      `smoke/windows.ps1` reads the binary's import table and fails if one
;      ever comes back.
;
;   2. The **Edge WebView2 runtime**, which draws the window. It is not an
;      import -- Tauri links the loader statically and asks for the runtime at
;      run time -- so a missing one is a failed window, not a dead process.
;      The Evergreen *bootstrapper* is bundled in this installer (see the
;      [Files] section) and run when the runtime is absent, rather than
;      downloaded at install time as it was before 7.35.2.
;
; BUILD
;   iscc /DAppVersion=6.45.0 ^
;        /DSourceExe=..\..\src-tauri\target\x86_64-pc-windows-msvc\release\MyClaudeCode.exe ^
;        /DWebView2Setup=C:\somewhere\MicrosoftEdgeWebview2Setup.exe ^
;        MyClaudeCode.iss
;   Built with Inno Setup 6.7.3 (pinned in .github/workflows/shell-release.yml).
;   `/DWebView2Setup` is optional for a syntax check and mandatory for a
;   release: without it the bootstrapper is not bundled and the installer falls
;   back to downloading it. The release workflow fetches the file, checks it is
;   Authenticode-signed by Microsoft, and refuses to compile without it.

#ifndef AppVersion
  ; A local `iscc MyClaudeCode.iss` with no /D still compiles, so the script
  ; can be syntax-checked without inventing a release. CI always passes one.
  #define AppVersion "0.0.0"
#endif

#ifndef SourceExe
  #define SourceExe "..\..\src-tauri\target\release\MyClaudeCode.exe"
#endif

; The Evergreen WebView2 bootstrapper to embed. The release workflow passes an
; absolute path to a freshly fetched copy; a bare `iscc MyClaudeCode.iss` with
; no /D finds nothing, does not embed, and compiles anyway -- it just produces
; an installer that downloads the bootstrapper instead of carrying it, which is
; what every release before 7.35.2 did.
#ifndef WebView2Setup
  #define WebView2Setup "MicrosoftEdgeWebview2Setup.exe"
#endif
#if FileExists(WebView2Setup)
  #define WebView2Bundled
#endif

#define AppName "My Claude Code"
#define AppExeName "MyClaudeCode.exe"
#define AppPublisher "My Claude Code"
#define AppUrl "https://github.com/FiredMosquito831/my-claude-code"

; The shell's own application-data directory, and the only thing this
; installer can be asked to delete outside its own program directory. It is
; `dirs::data_dir()/<identifier>` -- Tauri 2's `app_data_dir()`, which on
; Windows is FOLDERID_RoamingAppData -- with the identifier from
; `desktop-shell/src-tauri/tauri.conf.json`. It holds `window.json` and
; nothing else: geometry, no configuration, no credentials.
#define ShellDataDir "com.myclaudecode.desktop"

[Setup]
; Fixed forever. It is the upgrade key, the uninstall key name
; (`{AppId}_is1`) and the winget ProductCode. Changing it turns every future
; release into a second, parallel installation.
AppId={{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

; Per-user, no elevation. `dialog` lets a user who genuinely wants a
; machine-wide install ask for one; nothing here requires it.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes
UsePreviousAppDir=yes

; `x64compatible` rather than `x64`: it is true on x64 and on Arm64 running
; x64 code, which is what a x86_64 binary actually needs. (Inno Setup 6.3+.)
ArchitecturesInstallIn64BitMode=x64compatible

Uninstallable=yes
; The name in Apps & Features. It says "desktop app" out loud because
; removing it is NOT removing My Claude Code the server -- see the header.
UninstallDisplayName={#AppName} (desktop app)
UninstallDisplayIcon={app}\{#AppExeName}

OutputDir=..\..\..\dist
OutputBaseFilename=MyClaudeCode-Setup-windows-x86_64
SetupIconFile=..\..\src-tauri\icons\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no

; The shell is a single-instance app; an in-place upgrade must be able to
; replace a running exe. Restart Manager only ever closes files this setup is
; installing, so this cannot reach the server or the tray.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Off by default: a Start Menu entry is enough, and an unasked-for desktop
; icon is the most common complaint about Windows installers.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Both entries are ordinary installed files, so Inno's own uninstall log
; removes them. Nothing here carries `uninsneveruninstall`, and the parity
; contract asserts that.
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion
Source: "..\..\src-tauri\icons\icon.ico"; DestDir: "{app}"; DestName: "app-icon.ico"; Flags: ignoreversion
#ifdef WebView2Bundled
; The WebView2 Evergreen bootstrapper (~1.8 MB), carried inside this setup and
; never installed: `dontcopy` means it has no DestDir, is not written into
; {app}, and is not in the uninstall log -- [Code] extracts it to {tmp} only on
; a machine whose runtime is missing, and {tmp} is removed when setup exits.
; There is therefore nothing for the uninstaller to remove, which is why this
; is the one [Files] entry with no "DestDir: {app}" on it.
Source: "{#WebView2Setup}"; Flags: dontcopy noencryption
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app-icon.ico"; Comment: "The My Claude Code dashboard, in its own window."
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app-icon.ico"; Tasks: desktopicon

; There is deliberately no [Registry] section. Inno writes its own
; HKCU\...\Uninstall\{AppId}_is1 key and removes it; anything else this file
; wrote would be a registry value with no owner. Autostart in particular
; belongs to the application (see the header).

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Inno removes every [Files] and [Icons] entry from its log, and removes
; {app} and {group} when they end up empty. This catches what a *run* of the
; app leaves behind next to the exe -- a WebView2 user-data directory, which
; the webview creates on first paint and nothing in the install log knows
; about.
Type: filesandordirs; Name: "{app}\EBWebView"
Type: filesandordirs; Name: "{app}\{#AppExeName}.WebView2"
Type: dirifempty; Name: "{app}"

[Code]

// ------------------------------------------------------------------
// WebView2 detection.
//
// Microsoft's documented check: the Evergreen Runtime registers itself under
// the EdgeUpdate client GUID F3017226-FE2A-4295-8BDF-00C3A9A7E4C5 with a
// `pv` version string. A per-machine install writes HKLM (under WOW6432Node
// on 64-bit Windows, which is what the HKLM32 view reads); a per-user install
// writes HKCU. A `pv` that is absent, empty or "0.0.0.0" means not installed.
//
// (These are `//` comments and not Pascal `{ }` ones on purpose: a brace
// comment ends at the first `}`, and every interesting name in this file --
// a GUID, an Inno constant like {app} -- contains one.)
//
// Windows 11 ships the runtime as part of the OS and Microsoft pushed it to
// Windows 10 from December 2022, so on nearly every machine this function
// returns True and nothing is installed.
//
// A `pv` value on its own is NOT taken as proof any more. A registry entry
// whose files are gone -- a half-removed runtime, an image built by deleting
// the directory, a reset that left the key behind -- would make this installer
// skip the one step that machine needed. So the version read out of `pv` has
// to name a directory that actually holds `msedgewebview2.exe`, in one of the
// two places the runtime installs into: `%ProgramFiles(x86)%\Microsoft\
// EdgeWebView\Application\<pv>` per machine, `%LOCALAPPDATA%\...` per user.
// A false "absent" costs a bootstrapper run that exits immediately; a false
// "present" costs a window that never opens.
// ------------------------------------------------------------------

const
  WebView2ClientKey =
    'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

  // The Evergreen *Bootstrapper*: ~1.8 MB, installs whatever the current
  // runtime is. Since 7.35.2 it is EMBEDDED in this setup ([Files], `dontcopy`)
  // and only downloaded from this URL when a build did not embed one. Its
  // SHA-256 is deliberately NOT pinned, and cannot be: this is a rolling file
  // whose bytes change every time Microsoft ships a runtime, so a pin would
  // turn every runtime release into a broken build. What CI verifies instead
  // is that the copy it fetched carries a valid Microsoft Authenticode
  // signature. The alternative -- the Fixed Version runtime -- is 130+ MB per
  // architecture, never auto-updates, and would have to be re-cut by hand for
  // every Chromium CVE.
  WebView2BootstrapperUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';
  WebView2BootstrapperFile = 'MicrosoftEdgeWebview2Setup.exe';
  WebView2RuntimeSubdir = '\Microsoft\EdgeWebView\Application\';

function VersionIsInstalled(const Value: string): Boolean;
begin
  Result := (Trim(Value) <> '') and (Trim(Value) <> '0.0.0.0');
end;

function RuntimeFilesExist(const Version: string): Boolean;
begin
  Result :=
    FileExists(ExpandConstant('{commonpf32}') + WebView2RuntimeSubdir + Version +
               '\msedgewebview2.exe') or
    FileExists(ExpandConstant('{localappdata}') + WebView2RuntimeSubdir + Version +
               '\msedgewebview2.exe');
end;

function RuntimeRegisteredAt(const RootKey: Integer; var Version: string): Boolean;
begin
  Result := RegQueryStringValue(RootKey, WebView2ClientKey, 'pv', Version) and
            VersionIsInstalled(Version) and
            RuntimeFilesExist(Trim(Version));
end;

function WebView2RuntimeInstalled(var Version: string): Boolean;
begin
  Result := True;
  if RuntimeRegisteredAt(HKEY_LOCAL_MACHINE_32, Version) then
    Exit;
  if IsWin64 and RuntimeRegisteredAt(HKEY_LOCAL_MACHINE_64, Version) then
    Exit;
  if RuntimeRegisteredAt(HKEY_CURRENT_USER, Version) then
    Exit;
  Result := False;
end;

// Put the bootstrapper in {tmp}, from the copy inside this setup when there is
// one and from Microsoft's permanent fwlink when there is not.
function ObtainBootstrapper(var Target: string; var Problem: string): Boolean;
begin
  Result := False;
  Target := ExpandConstant('{tmp}\' + WebView2BootstrapperFile);
#ifdef WebView2Bundled
  try
    ExtractTemporaryFile(WebView2BootstrapperFile);
  except
    Problem := 'the bundled WebView2 runtime installer could not be unpacked (' +
      GetExceptionMessage + ')';
    Exit;
  end;
  Log('WebView2: using the bundled bootstrapper');
#else
  try
    DownloadTemporaryFile(WebView2BootstrapperUrl, WebView2BootstrapperFile, '', nil);
  except
    Problem := 'the WebView2 runtime installer could not be downloaded (' +
      GetExceptionMessage + ')';
    Exit;
  end;
  Log('WebView2: downloaded the bootstrapper');
#endif
  Result := True;
end;

function InstallWebView2Runtime(var Problem: string): Boolean;
var
  Target: string;
  ResultCode: Integer;
begin
  Result := False;
  if not ObtainBootstrapper(Target, Problem) then
    Exit;
  // `/silent /install` is Microsoft's documented pair for the bootstrapper.
  // It elevates on its own when it needs to; a per-user runtime install does
  // not.
  if not Exec(Target, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Problem := 'the WebView2 runtime installer could not be started';
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Problem := Format('the WebView2 runtime installer exited with code %d', [ResultCode]);
    Exit;
  end;
  Result := True;
end;

// Setup ignores switches it does not know, so this is how a private one is
// read back. Inno has no CmdLineParamExists.
function SwitchGiven(const Name: string): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
    if CompareText(ParamStr(Index), Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

// `/VERIFYWEBVIEW2` -- a switch for the release smoke, not for users.
//
// On the runners (and on almost every real machine) the runtime is already
// there, so the bundled bootstrapper is never unpacked and nothing proves it
// is actually inside the setup. This unpacks it and writes its size to the
// install log, so `smoke/windows-installer.ps1` can assert on every release
// that the file shipped, on the same silent install it already runs. It never
// installs anything and never fails setup.
procedure VerifyBundledBootstrapper();
var
  Size: Integer;
begin
#ifdef WebView2Bundled
  try
    ExtractTemporaryFile(WebView2BootstrapperFile);
  except
    Log('WebView2: bundled bootstrapper FAILED to unpack: ' + GetExceptionMessage);
    Exit;
  end;
  if FileSize(ExpandConstant('{tmp}\' + WebView2BootstrapperFile), Size) then
    Log(Format('WebView2: bundled bootstrapper verified, %d bytes', [Size]))
  else
    Log('WebView2: bundled bootstrapper unpacked but could not be measured');
#else
  Log('WebView2: this setup carries no bundled bootstrapper');
#endif
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Problem: string;
  Version: string;
begin
  Result := '';
  if SwitchGiven('/VERIFYWEBVIEW2') then
    VerifyBundledBootstrapper();

  if WebView2RuntimeInstalled(Version) then
  begin
    Log('WebView2: the runtime is present (pv ' + Trim(Version) + ')');
    Exit;
  end;

  Log('WebView2: the runtime is absent; installing it');
  if InstallWebView2Runtime(Problem) then
  begin
    Log('WebView2: the runtime was installed');
    Exit;
  end;
  Log('WebView2: ' + Problem);

  // Not fatal, on purpose. Aborting here would mean a machine that is briefly
  // offline cannot install the app at all, and the runtime may arrive later
  // through Windows Update. The window will say what is wrong if it does not.
  // In a /VERYSILENT run SuppressibleMsgBox returns the default and continues,
  // so winget never hangs on a dialog.
  SuppressibleMsgBox(
    'My Claude Code needs the Microsoft Edge WebView2 runtime, and ' + Problem + '.' + #13#10#13#10 +
    'Setup will continue. If the window does not open, install the WebView2 runtime from' + #13#10 +
    'https://developer.microsoft.com/microsoft-edge/webview2/ and start My Claude Code again.',
    mbInformation, MB_OK, IDOK);
end;

// ------------------------------------------------------------------
// Uninstall: the opt-in half.
//
// The exe, the icons, the shortcuts and the Apps & Features entry go
// automatically -- they are in Inno's uninstall log. The shell's
// window-geometry directory is the one thing a user might want to keep across
// a reinstall, so it is asked for rather than assumed.
//
// This is [Code] and not an [UninstallDelete] entry because [UninstallDelete]
// is recorded into the log at *install* time: a `Check:` on it would ask the
// question months before the answer matters. SuppressibleMsgBox defaults to
// "keep" under /VERYSILENT, so an unattended uninstall is conservative and
// never blocks.
// ------------------------------------------------------------------

function ShellDataDirectory(): string;
begin
  Result := ExpandConstant('{userappdata}\{#ShellDataDir}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  if not DirExists(ShellDataDirectory()) then
    Exit;
  if SuppressibleMsgBox(
       'Also delete the desktop app''s remembered window size and position?' + #13#10#13#10 +
       ShellDataDirectory() + #13#10#13#10 +
       'This does not affect My Claude Code itself, your configuration, or your providers.',
       mbConfirmation, MB_YESNO, IDNO) = IDYES then
    DelTree(ShellDataDirectory(), True, True, True);
end;
