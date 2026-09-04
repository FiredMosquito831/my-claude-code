# Submitting the manifest to `microsoft/winget-pkgs`

**Nothing in this directory has been submitted.** Publishing My Claude Code to
the Windows Package Manager community repository puts the project's name in a
Microsoft-run index, under a package identifier that is claimed permanently and
is awkward to rename. That is the owner's call, not an implementation detail, so
the manifests, the validation and the install/uninstall proof are all done and
this file is where it stops. Nothing under `microsoft/winget-pkgs` has been
forked, branched or opened.

When the answer is yes, everything below is ready to run.

---

## 0. What already holds

These are not predictions. They were measured on Windows 11 26100 with winget
v1.29.290 against the real `v6.45.2` release asset, and the three moderator
requirements are exactly what they check:

| Moderator requirement | Evidence |
| --- | --- |
| The installer must install **silently**. | `MyClaudeCode-Setup-windows-x86_64.exe /SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` — the literal switch set winget supplies for `InstallerType: inno` (`winget-cli`, `src/AppInstallerCommonCore/Manifest/ManifestCommon.cpp`, `GetDefaultKnownSwitches`) — exits `0` with no prompt and no elevation. |
| **Uninstall must work**, silently. | winget runs `QuietUninstallString`, which this installer registers as `"…\unins000.exe" /SILENT`. Running exactly that exits `0` and removes the program directory, the Start Menu shortcut and the Apps & Features key. |
| The **`ProductCode` must match the Apps & Features entry**. | With the app installed, `winget list --name "My Claude Code"` reports its id as `ARP\User\X64\{5FC8D5C3-33F7-4366-AD8D-C844D21BC089}_is1` — which is `Scope: user` + `Architecture: x64` + the manifest's `ProductCode`, character for character. That is the correlation winget will make once the identifier exists in a source. |
| `winget validate` passes. | `winget validate --manifest desktop-shell/installer/winget/6.45.2` → *Manifest validation succeeded.* |
| Uninstalling leaves nothing behind. | `HKCU\…\Uninstall`, `HKCU\…\Run`, both Start Menu Programs trees, `%LOCALAPPDATA%\Programs` and `~/.local/bin` were snapshotted before the install and diffed after the uninstall. All five diffs were empty. |

One thing to say out loud in the pull request rather than let a moderator find:
**the installer is unsigned**, and it will stay unsigned (decision Q9 — even an
EV certificate no longer skips SmartScreen). winget-pkgs accepts unsigned
installers; it does not accept ones that prompt.

## 1. Where the files go

The three manifests in `6.45.2/` beside this file are the submission, unchanged.
Copy them to:

```
manifests/f/FiredMosquito831/MyClaudeCode/6.45.2/
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

## 2. Two ways to submit

### 2a. `wingetcreate` (recommended, and what future versions should use)

```powershell
winget install Microsoft.WingetCreate
wingetcreate submit --token <a GitHub PAT with public_repo> `
    desktop-shell\installer\winget\6.45.2
```

`wingetcreate submit` forks `microsoft/winget-pkgs` into the token's account,
branches, commits the manifests into the right path, and opens the pull request.
For every release *after* the first, `wingetcreate update` is one command:

```powershell
wingetcreate update FiredMosquito831.MyClaudeCode `
    --version 6.46.0 `
    --urls https://github.com/FiredMosquito831/my-claude-code/releases/download/v6.46.0/MyClaudeCode-Setup-windows-x86_64.exe `
    --submit --token <PAT>
```

It downloads the installer, computes the hash itself and carries every other
field forward. Run `render.py` anyway and diff, so the in-repo copy stays the
source of truth.

`komac update FiredMosquito831.MyClaudeCode --version 6.46.0 --urls <url>
--submit` does the same job and is the tool most community publishers have moved
to; either is fine.

### 2b. By hand

1. Fork `microsoft/winget-pkgs` **to the submitter's own account** — never to
   this project's organisation, and never push to `microsoft/winget-pkgs`
   itself.
2. `git checkout -b FiredMosquito831.MyClaudeCode-6.45.2`
3. Copy the three files into the path in §1.
4. Commit: `New package: FiredMosquito831.MyClaudeCode version 6.45.2`
5. Push and open one pull request. **One package version per pull request** —
   that rule is enforced.

## 3. The pull request text, ready to paste

**Title**

```
New package: FiredMosquito831.MyClaudeCode version 6.45.2
```

**Body**

```markdown
### Package
`FiredMosquito831.MyClaudeCode` 6.45.2 — the My Claude Code desktop app, a
small native window (~3.5 MB installed) onto the dashboard the project's local
server already serves on 127.0.0.1.

Homepage: https://github.com/FiredMosquito831/my-claude-code
Installer: https://github.com/FiredMosquito831/my-claude-code/releases/download/v6.45.2/MyClaudeCode-Setup-windows-x86_64.exe

### Checklist
- [x] Have you signed the [Contributor License Agreement](https://cla.opensource.microsoft.com/microsoft/winget-pkgs)?
- [x] Have you checked that there aren't other open [pull requests](https://github.com/microsoft/winget-pkgs/pulls) for the same manifest update/add?
- [x] Have you validated your manifest locally with `winget validate --manifest <path>`?
- [x] Have you tested your manifest locally with `winget install --manifest <path>`?
- [x] Does your manifest conform to the [1.28 schema](https://github.com/microsoft/winget-pkgs/tree/master/doc/manifest/schema/1.28.0)?

### Notes for the reviewer
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

## 4. After it is accepted

1. Add the badge / one-liner to the README's Windows row — the text is already
   written there, gated on "once the manifest is accepted".
2. Every subsequent release needs a new version folder. The repository step is
   one command (`render.py v<N>`), documented in
   `docs/RELEASE-CHECKLIST.md` §9; the submission is `wingetcreate update` or
   `komac update`.
3. `winget` will then be able to *upgrade* installations, because
   `AppsAndFeaturesEntries.DisplayVersion` is the version Inno stamps, which is
   the tag without its `v`.

## 5. What must not happen

- Do not fork, branch, push to or open anything under `microsoft/winget-pkgs`
  without the owner saying so explicitly. This file exists so that when they do,
  nothing has to be worked out under time pressure.
- Do not submit a version whose release assets are not final. The
  `InstallerSha256` is checked on every install; re-uploading an asset after
  submission breaks every install of that version, and the fix is a new
  manifest version, not an edit.
- Do not hand-edit the files in `6.45.2/`. They are rendered; edit `render.py`
  and re-run it, or `tests/scripts/test_winget_manifest.py` fails.
