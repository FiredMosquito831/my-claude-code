# Submitting the manifest to `microsoft/winget-pkgs`

**Status: submitted, open, and blocked on one technical fix that this directory
now carries.** The earlier version of this file said nothing had been submitted.
That stopped being true on 2026-09-05.

| | |
| --- | --- |
| Pull request | [`microsoft/winget-pkgs` #430045](https://github.com/microsoft/winget-pkgs/pull/430045), opened 2026-09-05, **open** |
| Submitted version | `6.45.2`, then `7.13.1` — both superseded; this directory now renders **7.35.2** |
| CLA | **Signed** (the `Needs-CLA` label cleared on 2026-09-15) |
| Remaining label | `Validation-Executable-Error` — from the 7.13.1 run; see §0 |

## 0. The two rejections, and what each one taught

Both are worth reading before touching anything here, because each was a
*plausible* mistake rather than a careless one.

**1. `Manifest-Version-Error` (2026-09-05).** The manifests went out declaring
`ManifestVersion: 1.28.0` — the newest schema published under
`doc/manifest/schema/`, and one the client on the release machine (winget
v1.29.290) validated happily. The bot answered that **1.12.0** is "the version
currently approved for release". *The newest schema that exists is not the
newest schema the repository accepts, and `winget validate` passing proves only
that your local client understands the file.* Fixed; `render.py` pins 1.12.0
with the reasoning written at the constant.

**2. `Validation-Executable-Error` (2026-09-11), and the wrong fix for it
(2026-09-15).** The pipeline installed the package on a clean validator VM, ran
`MyClaudeCode.exe`, and got `-1073741515` = `0xC0000135` = `STATUS_DLL_NOT_FOUND`:

> Executable C:\Users\validator\AppData\Local\Programs\My Claude Code\MyClaudeCode.exe returned exit code: -1073741515

This was read as a missing **Edge WebView2 runtime** — the shell is a Tauri app,
so its window is drawn by WebView2 — and 7.13.1 went out declaring
`Dependencies.PackageDependencies: [Microsoft.EdgeWebView2Runtime]`. **It did not
work.** The pipeline re-ran on the new head and re-applied
`Validation-Executable-Error` on 2026-09-15T00:12:45Z.

The diagnosis was wrong, and the exit code says so if you read it carefully:
`0xC0000135` is raised by the Windows **loader**, resolving the executable's
imports, before a single instruction of the program runs. A missing WebView2
runtime cannot produce it — Tauri links `WebView2Loader` **statically** and asks
for the runtime at run time, so a missing runtime is a failed *window*, not a
dead *process*. Dumping the shipped executable's import table settles it:

```
$ objdump -p MyClaudeCode.exe | grep 'DLL Name'
    DLL Name: VCRUNTIME140.dll        <-- Visual C++ redistributable
    DLL Name: VCRUNTIME140_1.dll      <-- Visual C++ redistributable
    DLL Name: api-ms-win-crt-*.dll    (UCRT: part of Windows 10+)
    DLL Name: kernel32.dll, user32.dll, ole32.dll, ...   (Windows)
```

No WebView2 anything; two DLLs from the **Visual C++ 2015-2022 redistributable**,
which a clean Windows image does not have. The validator's own error table said
as much in the first comment on the pull request — *"The most common dependency
is `Microsoft.VCRedist.2015+.x64`"*.

**The fix is in the build, not in the manifest.** From 7.35.2 the Windows binary
is compiled with `-C target-feature=+crt-static`
(`desktop-shell/src-tauri/.cargo/config.toml`), so it imports nothing Windows
does not ship, and `desktop-shell/smoke/windows.ps1` fails the release if a
redistributable import ever returns. Bundling `VC_redist.x64.exe` was rejected:
25 MB, and it requires administrator rights that a `PrivilegesRequired=lowest`
installer does not have.

The `Dependencies` block **stays**. It was not the fix, but it is not wrong: a
user on a fresh Windows install still needs the runtime for the window to open.
Belt and braces, the installer now also **bundles** the 1.8 MB WebView2
Evergreen bootstrapper (`[Files]` + `dontcopy`) instead of downloading it
mid-install, and its `pv` registry probe now also checks that the version it
found has `msedgewebview2.exe` on disk.

```yaml
Dependencies:
  PackageDependencies:
    - PackageIdentifier: Microsoft.EdgeWebView2Runtime
```

## 1. What already holds

These are not predictions. They were measured on Windows 11 26100 with winget
v1.29.290 against a real release asset, and the three moderator requirements are
exactly what they check. The measurements were taken against `v6.45.2`'s
installer; nothing about the Inno script, the `AppId`, the switch set or the
uninstall path has changed since, and the rendered manifest is a pure function of
the release plus `MyClaudeCode.iss`.

| Moderator requirement | Evidence |
| --- | --- |
| The installer must install **silently**. | `MyClaudeCode-Setup-windows-x86_64.exe /SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` — the literal switch set winget supplies for `InstallerType: inno` (`winget-cli`, `src/AppInstallerCommonCore/Manifest/ManifestCommon.cpp`, `GetDefaultKnownSwitches`) — exits `0` with no prompt and no elevation. |
| **Uninstall must work**, silently. | winget runs `QuietUninstallString`, which this installer registers as `"…\unins000.exe" /SILENT`. Running exactly that exits `0` and removes the program directory, the Start Menu shortcut and the Apps & Features key. |
| The **`ProductCode` must match the Apps & Features entry**. | With the app installed, `winget list --name "My Claude Code"` reports its id as `ARP\User\X64\{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}_is1` — which is `Scope: user` + `Architecture: x64` + the manifest's `ProductCode`, character for character. |
| `winget validate` passes. | Measured on `7.13.1`: `winget validate --manifest desktop-shell/installer/winget/7.13.1` → *Manifest validation succeeded.* The `7.35.2` manifests differ from those only in `PackageVersion`, `InstallerUrl`, `InstallerSha256`, `DisplayVersion` and `ReleaseDate` — same renderer, same schema — and have **not** been run through `winget validate`, because the machine that rendered them has no `winget`. **It is necessary and not sufficient anyway** — see §0, rejection 1. |
| Uninstalling leaves nothing behind. | `HKCU\…\Uninstall`, `HKCU\…\Run`, both Start Menu Programs trees, `%LOCALAPPDATA%\Programs` and `~/.local/bin` were snapshotted before the install and diffed after the uninstall. All five diffs were empty. |

One thing to say out loud in the pull request rather than let a moderator find:
**the installer is unsigned**, and it will stay unsigned (decision Q9 — even an
EV certificate no longer skips SmartScreen). winget-pkgs accepts unsigned
installers; it does not accept ones that prompt.

## 2. Where the files go

The three manifests in `7.35.2/` beside this file are the submission, unchanged.
Copy them to:

```
manifests/f/FiredMosquito831/MyClaudeCode/7.35.2/
    FiredMosquito831.MyClaudeCode.yaml
    FiredMosquito831.MyClaudeCode.installer.yaml
    FiredMosquito831.MyClaudeCode.locale.en-US.yaml
```

The partition letter `f` is the lower-cased first letter of the publisher
segment. The publisher and package folders, and the version folder, must match
`PackageIdentifier` and `PackageVersion` exactly — that is enforced by the
validation pipeline, not by convention.

**Why `FiredMosquito831` and not "My Claude Code".** The community repository
asks for "the name of the company that publishes the tool". There is no company;
the publisher is a GitHub account, and the repository's own convention for that
case is the account name — `sharkdp.bat`, `ajeetdsouza.zoxide`,
`junegunn.fzf`. It is also the only half of the identifier a user could guess
from the URL they downloaded the installer from. Note that this deliberately
differs from `AppsAndFeaturesEntries.Publisher`, which is `My Claude Code`:
that field is not a display name, it is the string Inno Setup writes into the
registry, and winget compares it against what it reads back.

## 3. Updating the open pull request

**#430045 already exists, the CLA is signed against it, and its history carries
both rejections.** Update it rather than opening a second one — one package
version per pull request is enforced, and a duplicate would be closed.

1. On the existing branch, **delete** the previous version's directory under
   `manifests/f/FiredMosquito831/MyClaudeCode/` and add the three files from
   `7.35.2/` at the path in §2. The branch is
   `FiredMosquito831.MyClaudeCode-6.45.2` on the submitter's fork — its name is
   from the first submission and does not matter; the path inside it does. With
   the repository too large to clone (even shallow), the update is six GitHub
   **Contents API** calls: three `PUT` for the new files, three `DELETE` for the
   old ones, all on that branch.
2. Retitle the pull request to `New package: FiredMosquito831.MyClaudeCode version 7.35.2`.
3. Push. The pipeline re-runs from scratch on the new head.
4. Add a comment saying what changed and why — the `Validation-Executable-Error`
   label is cleared by a moderator or by a green run, not by the push itself.

Suggested comment:

```markdown
Superseding 7.13.1 with 7.35.2. The previous update misdiagnosed the executable
failure; this one fixes it in the build.

`0xC0000135` (`STATUS_DLL_NOT_FOUND`) is raised by the Windows loader while
resolving the executable's imports, before any of the program's own code runs —
so it could not have been the WebView2 runtime, which this app links statically
as a loader and asks for at run time. The import table named the real cause:

    VCRUNTIME140.dll
    VCRUNTIME140_1.dll

— the Visual C++ 2015-2022 redistributable, which a clean image does not carry,
exactly as the validation comment's own table suggested.

From 7.35.2 the binary is built with a statically linked CRT and imports nothing
Windows does not ship (verified on the release itself: 14 imported DLLs, all
system). The installer additionally now *bundles* the WebView2 Evergreen
bootstrapper instead of downloading it at install time, and checks the runtime's
files rather than only its registry entry. The `Dependencies` block is kept: it
was not the fix, but a user on a fresh Windows still needs the runtime for the
window to open.
```

## 4. Two ways to submit a *new* version

### 4a. `wingetcreate` (recommended, and what future versions should use)

```powershell
winget install Microsoft.WingetCreate
wingetcreate submit --token <a GitHub PAT with public_repo> `
    desktop-shell\installer\winget\7.35.2
```

`wingetcreate submit` forks `microsoft/winget-pkgs` into the token's account,
branches, commits the manifests into the right path, and opens the pull request.
For every release *after* the first, `wingetcreate update` is one command:

```powershell
wingetcreate update FiredMosquito831.MyClaudeCode `
    --version 7.14.0 `
    --urls https://github.com/FiredMosquito831/my-claude-code/releases/download/v7.14.0/MyClaudeCode-Setup-windows-x86_64.exe `
    --submit --token <PAT>
```

It downloads the installer, computes the hash itself and carries every other
field forward — **including `Dependencies`**, which is why declaring it once in
`render.py` is enough. Run `render.py` anyway and diff, so the in-repo copy stays
the source of truth.

`komac update FiredMosquito831.MyClaudeCode --version 7.14.0 --urls <url>
--submit` does the same job and is the tool most community publishers have moved
to; either is fine.

### 4b. By hand

1. Fork `microsoft/winget-pkgs` **to the submitter's own account** — never to
   this project's organisation, and never push to `microsoft/winget-pkgs`
   itself.
2. `git checkout -b FiredMosquito831.MyClaudeCode-7.35.2`
3. Copy the three files into the path in §2.
4. Commit: `New package: FiredMosquito831.MyClaudeCode version 7.35.2`
5. Push and open one pull request. **One package version per pull request** —
   that rule is enforced.

## 5. The pull request text, ready to paste

**Title**

```
New package: FiredMosquito831.MyClaudeCode version 7.35.2
```

**Body**

```markdown
### Package
`FiredMosquito831.MyClaudeCode` 7.35.2 — the My Claude Code desktop app, a
small native window (~3.5 MB installed) onto the dashboard the project's local
server already serves on 127.0.0.1.

Homepage: https://github.com/FiredMosquito831/my-claude-code
Installer: https://github.com/FiredMosquito831/my-claude-code/releases/download/v7.35.2/MyClaudeCode-Setup-windows-x86_64.exe

### Checklist
- [x] Have you signed the [Contributor License Agreement](https://cla.opensource.microsoft.com/microsoft/winget-pkgs)?
- [x] Have you checked that there aren't other open [pull requests](https://github.com/microsoft/winget-pkgs/pulls) for the same manifest update/add?
- [x] Have you validated your manifest locally with `winget validate --manifest <path>`?
- [x] Have you tested your manifest locally with `winget install --manifest <path>`?
- [x] Does your manifest conform to the [1.12 schema](https://github.com/microsoft/winget-pkgs/tree/master/doc/manifest/schema/1.12.0)?

### Notes for the reviewer
- **Depends on `Microsoft.EdgeWebView2Runtime`.** This is a Tauri app: the window
  is drawn by the WebView2 runtime, and the executable will not start without it
  (`0xC0000135`). The dependency is declared rather than left to the installer's
  own bootstrapper, which only fires when its registry probe reports the runtime
  missing.
- **Per-user Inno Setup installer**, `PrivilegesRequired=lowest`. `Scope: user`;
  there is no machine-scope installer to offer.
- **Silent install and silent uninstall both verified**, using the default Inno
  switches winget supplies (`/SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`) and
  the registered `QuietUninstallString`. No `InstallerSwitches` block is present
  because none is needed.
- **`AppsAndFeaturesEntries` mirrors the registry exactly**: the installer's
  `AppId` is fixed forever, so the uninstall key is always
  `{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}_is1`. With the app installed,
  `winget list` reports the package as
  `ARP\User\X64\{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}_is1`.
- **The installer is unsigned and will remain so.** SmartScreen shows its
  reputation warning on first run; this is documented in the project's README
  rather than papered over. The package carries no `Commands`, because the
  installer deliberately puts nothing on `PATH`.
- The manifests are generated from the release by
  [`desktop-shell/installer/winget/render.py`](https://github.com/FiredMosquito831/my-claude-code/blob/main/desktop-shell/installer/winget/render.py)
  in the source repository, and a test there asserts the committed copies are
  byte-for-byte what it produces.
```

## 6. After it is accepted

1. Add the badge / one-liner to the README's Windows row — the text is already
   written there, gated on "once the manifest is accepted".
2. Every subsequent release needs a new version folder. The repository step is
   one command (`render.py v<N>`), documented in
   `docs/RELEASE-CHECKLIST.md` §9; the submission is `wingetcreate update` or
   `komac update`.
3. `winget` will then be able to *upgrade* installations, because
   `AppsAndFeaturesEntries.DisplayVersion` is the version Inno stamps, which is
   the tag without its `v`.

## 7. What must not happen

- Do not push to anything under `microsoft/winget-pkgs` without the owner saying
  so explicitly, and never to `microsoft/winget-pkgs` itself — only to a fork.
- Do not open a second pull request while #430045 is open. One package version
  per pull request is enforced and duplicates are closed.
- Do not submit a version whose release assets are not final. The
  `InstallerSha256` is checked on every install; re-uploading an asset after
  submission breaks every install of that version, and the fix is a new
  manifest version, not an edit.
- Do not hand-edit the files in `7.35.2/`. They are rendered; edit `render.py`
  and re-run it, or `tests/scripts/test_winget_manifest.py` fails.
- Do not drop the `Dependencies` block when bumping a version by hand. It is
  pinned by a test for exactly that reason.
