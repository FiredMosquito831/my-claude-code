# The My Claude Code desktop shell

A window and a tray icon. It renders `.../admin` — the dashboard the local MCC
server already serves — over real HTTP, in the operating system's own webview.

It is deliberately small. There is no product surface here: everything the user
sees inside the window is `admin.js`, served by `mcc-server`. When the dashboard
changes, this binary does not.

- **Framework:** Tauri v2 (Rust). Chosen for size (a hello-world is ~2.7–3 MB),
  for the OS webview rather than a bundled browser engine, and because the tray
  and the single-instance guard are first-party — see
  `specs/PR-DESKTOP-WEBVIEW-SPEC.md` §1.
- **Product name:** `My Claude Code`. **Executable:** `MyClaudeCode`, with no
  version in the name, so a Start Menu shortcut or a `.desktop` `Exec=` line
  never has to be rewritten on an upgrade. **Identifier:**
  `com.myclaudecode.desktop`.
- **No updater.** `tauri-plugin-updater` is not a dependency and the
  configuration declares no update endpoint and no public key. The dashboard's
  own updater stays the only updater there is.

## The one rule

**This shell never resolves the configuration directory, the port or the admin
URL.** It runs

```
mcc-desktop --print-status
```

and uses the strings that come back, verbatim. That is contract C1, and
`tests/contracts/test_config_dir_is_single_sourced.py::test_shell_source_never_names_the_config_dir`
enforces it by grepping this tree: a directory name, an environment variable
name or a default port number appearing anywhere under `desktop-shell/` fails
the Python test suite.

The reason is that resolving the configuration directory is a policy, not a
lookup — an environment override, then the current home, then a legacy home
*only if* four health checks pass, one of which opens a SQLite database and
compares its columns. A second implementation of that in Rust would work on the
author's machine and quietly ignore everyone else's override.

Two more things it must not do, for the same family of reasons:

- **It never takes `desktop.lock`.** Python's lock is a `msvcrt` byte-range lock
  on Windows and an `flock` elsewhere; a Rust lock on the same file is not
  portably interoperable with either, and getting it wrong yields two trays.
  This shell has its own guard (`tauri-plugin-single-instance`) on its own key.
- **It never writes `desktop.json`, an HKCU `Run` value, a LaunchAgent or an
  autostart file** (contract C4). Those go through `mcc-desktop` flags. The only
  thing this binary writes is its own `window.json`, in the OS application-data
  directory — geometry is a property of a display, not of an MCC install.

## The ladder

One pass, driven by `server_presence` in the status document. The decision is a
pure function (`src/ladder.rs::decide`), so every row below is a unit test.

| Status | What happens | What the user sees |
|---|---|---|
| `mcc-desktop` not on `PATH` | The project's own install script is run (decision Q4) | "Installing My Claude Code", the exact command, and its output line by line — then the ladder runs again |
| `healthy` | Attach. Nothing is started. | The dashboard |
| `free` + `server_mode: spawn` | `mcc-server` is started, then `health_url` is polled every `health_check_interval_seconds` until `start_timeout_seconds` | "Starting the server…", then the dashboard |
| `free` + `attach` / `off` | Nothing is started | "The server is not running. Server mode is *attach*; start `mcc-server` yourself, or switch to spawn." + Retry |
| `foreign` | Nothing is started | The `port_conflict` sentence Python wrote, verbatim — it names the holding process — + Retry |
| Was healthy, now failing, under `health_failure_threshold` | Nothing at all | The dashboard, untouched |
| …over the threshold, inside `reconnect_timeout_seconds` | Reconnect banner; the dashboard is reloaded the moment health returns | "The server stopped answering — it is probably restarting." |
| …past the budget, or a start that timed out | The end of the line | An error page naming `server_log`, + Retry |
| `schema` is not 1, or `server_presence` is a word this build does not know | Refuses | "Update the desktop window" — never a guess |

Every number in that table is read from the status document. None of them is
compiled into this binary (contract C9), which is what stops a routine server
update being painted over with an error page.

## Building

Everything below runs from `desktop-shell/src-tauri/`.

```
cargo test          # 42 unit tests, no window, no network, no MCC install
cargo clippy --all-targets -- -D warnings
cargo build         # debug
cargo tauri build --no-bundle    # release binary, no installer
```

The release binary lands at
`src-tauri/target/release/MyClaudeCode` (`.exe` on Windows).
Installers are a later PR (S5 for Windows, S6 for Linux); `--no-bundle` is what
this PR promises.

### Per-OS prerequisites

- **Windows** — a Rust toolchain with the MSVC target, the Visual Studio Build
  Tools, and the WebView2 Runtime. WebView2 is part of Windows 11; on Windows 10
  it has been pushed to eligible devices since December 2022, and the ~2 MB
  Evergreen Bootstrapper covers the rest.
- **Linux** — Ubuntu 22.04+, Debian 12+ or Fedora 40+ (the floor is
  webkit2gtk **4.1**; Ubuntu 20.04, Debian 11 and RHEL 8–9 have only 4.0 and are
  out of scope). On Debian and Ubuntu:

  ```
  sudo apt-get install libwebkit2gtk-4.1-dev libgtk-3-dev \
      libayatana-appindicator3-dev librsvg2-dev
  ```

- **macOS** — Xcode command line tools (`lipo`, `codesign`, `iconutil`,
  `hdiutil` and `plutil` all come from them). Still untested by hand: no macOS
  machine was available for any PR in this arc, so every macOS claim here is
  what CI asserts, not what somebody saw.

There is **no Node build step and no bundler**. `ui/` is one static HTML file,
which is the honest shape for an application whose job is to render a URL.

## Testing without touching a real install

Two environment variables exist for the smoke tests, and for nothing else:

| Variable | Effect |
|---|---|
| `MCC_SHELL_DESKTOP_COMMAND` | The command run instead of `mcc-desktop`. May carry arguments. |
| `MCC_SHELL_SERVER_COMMAND` | The command run instead of `mcc-server`. |
| `MCC_SHELL_DATA_DIR` | Where `window.json` is read and written. |

Point the first at a script that prints a status document for a scratch server,
the third at a scratch directory, and a real window can be exercised end to end
without reading — or disturbing — the configuration and geometry a developer is
actually using.

## The pages this shell serves itself

Splash, "starting the server", "installing MCC", the port-conflict page, the
reconnect banner and the error page are all one bundled document, `ui/index.html`.
**None of them makes a network request** (contract C8): the admin API's
`require_loopback_admin` rejects a `file://` origin, so a page the shell serves
itself could not call the API even if it wanted to. Everything they display was
pushed in from Rust, which read it from `--print-status`.

The page talks back through exactly one command, `shell_retry`, because a Retry
button is the only thing a user can ask of this window. The remote dashboard is
granted no capability at all: it is rendered, never talked to.

## Testing the release build itself

Three scripts under `smoke/` run the *real* binary. They are what
`shell-release.yml` runs on each platform after `cargo test`, and they are the
only tests in this tree that open a window.

```
smoke/windows.ps1 -Binary src-tauri/target/x86_64-pc-windows-msvc/release/MyClaudeCode.exe
smoke/linux.sh      src-tauri/target/x86_64-unknown-linux-gnu/release/MyClaudeCode
smoke/macos.sh      src-tauri/target/aarch64-apple-darwin/release/MyClaudeCode
```

Three more smoke the *installers* rather than the binary. They snapshot, install,
assert, uninstall and diff:

```
smoke/windows-installer.ps1 -Setup ../dist/MyClaudeCode-Setup-windows-x86_64.exe
smoke/linux-deb.sh              ../dist/MyClaudeCode-linux-x86_64.deb
smoke/linux-tarball.sh          ../dist/MyClaudeCode-linux-x86_64.tar.gz
smoke/macos-dmg.sh              ../dist/MyClaudeCode-macos-universal.dmg
```

`macos-dmg.sh` needs neither root nor a display: a `.dmg` is not an installer,
so it mounts the image read-only, asserts the bundle, runs the program out of a
copy, detaches, and diffs `/Applications` and `~/Applications` back — both of
which it must never have touched. It is the only smoke here whose *expected*
result includes a failing command; see **The macOS disk image** below.

`linux-deb.sh` needs passwordless `sudo` (a runner has it; a laptop usually
does not) and re-runs `smoke/linux.sh` against the *installed*
`/usr/bin/MyClaudeCode`, so a package that ships a file list but not a working
program fails. `linux-tarball.sh` needs nothing at all: it points `HOME` at a
scratch directory, which is the whole claim `install-desktop.sh` makes.

Each one, in order: checks the artifact exists; stands up a fake `mcc-desktop`
and asserts *its* exit codes (`0` and a `schema: 1` document for
`--print-status`, `3` for anything else); launches the real binary against that
fake; waits for the shell to have run it; asserts the window is still up;
asserts that **nothing was started** — the fake status says
`server_presence: foreign`, so the port-conflict page is the whole of the
ladder and the fake `mcc-server` must never run; then stops the shell by its
exact process id and asserts it went.

Everything is pointed at a scratch directory through
`MCC_SHELL_DESKTOP_COMMAND`, `MCC_SHELL_SERVER_COMMAND` and
`MCC_SHELL_DATA_DIR`, so running a smoke on a machine that has a real MCC
install does not read or write that install, contact a port, or touch the
geometry of the window its owner actually uses. No process is ever stopped by
name.

Per-platform differences, and why:

- **Linux** starts `Xvfb` itself rather than using `xvfb-run`, so the pid the
  smoke signals is the shell's own and not a wrapper's, and exports
  `WEBKIT_DISABLE_COMPOSITING_MODE` and `WEBKIT_DISABLE_DMABUF_RENDERER` —
  without them WebKitGTK aborts on a virtual framebuffer and no window appears,
  silently. It also fails early, naming the package, if webkit2gtk **4.1** is
  absent.
- **macOS** needs no display, and adds `codesign --verify --strict`. On Apple
  silicon all code must carry at least an ad-hoc signature; a broken seal is
  reported to the user as "damaged", which no log explains. On x86_64 the
  absence of a signature is noted and permitted.
- **Windows** passes the fake through `cmd /c`, because `Command::new` cannot
  start a `.cmd` directly, and therefore requires a scratch path with no
  spaces (`MCC_SHELL_DESKTOP_COMMAND` is split on whitespace so it can carry
  arguments).

What a smoke cannot prove, and what the PR should not claim: that the window
*looks* right, that the tray icon appears (a CI image runs no status-area
host), or how SmartScreen and Gatekeeper greet a first launch — reputation is
per file hash and accrues from real download volume.

## The release layout

The shell is built by `.github/workflows/shell-release.yml`, which runs on
`release: published` and can be re-run for any existing tag with
`workflow_dispatch`.

The dispatch takes two inputs, because "where the assets go" and "what gets
built" are not always the same question. `tag` is the release to upload to.
`ref` is what to build, and defaults to `tag` — which is what a real release
wants, since the tag carries the source that release is made of. Backfilling a
release published *before* the shell existed needs them separated:

```
gh workflow run shell-release.yml --repo FiredMosquito831/my-claude-code \
    -f tag=v6.43.0 -f ref=main
```

Without `ref` that dispatch checks out a tree with no `desktop-shell/` in it,
and the workflow says so in one line rather than failing four legs on a missing
directory.

There is one release stream (decision Q6): the shell's
archives attach to the **same** GitHub release as the Python wheel. That is
safe because the updater selects the first asset whose name ends `.whl` and is
blind to everything else on a release —
`tests/application/test_release_updates_ignores_shell_assets.py` pins it.

| Runner | Rust target | Asset |
| --- | --- | --- |
| `windows-latest` | `x86_64-pc-windows-msvc` | `MyClaudeCode-windows-x86_64.zip` |
| `ubuntu-22.04` | `x86_64-unknown-linux-gnu` | `MyClaudeCode-linux-x86_64.tar.gz` |
| `macos-latest` | `aarch64-apple-darwin` | `MyClaudeCode-macos-aarch64.tar.gz` |
| `macos-15-intel` | `x86_64-apple-darwin` | `MyClaudeCode-macos-x86_64.tar.gz` |

The Windows and Linux legs each produce one more file from the same binary:
`MyClaudeCode-Setup-windows-x86_64.exe` and `MyClaudeCode-linux-x86_64.deb`,
the downloadable installers (delivery path B). They are described in **The
Windows installer** and **The Linux packages** below.

A fifth job, `macos-dmg`, runs after the matrix rather than inside it. It
downloads both macOS legs' binaries, merges them with `lipo` into one universal
executable, and produces `MyClaudeCode-macos-universal.dmg` — the macOS half of
delivery path B. It has to be a separate job: the two macOS legs are two
machines running at the same time, and `lipo` needs both to have finished,
which is what `needs: build` says and what a matrix leg cannot. It compiles
nothing, so it costs two or three minutes of a macOS runner. See **The macOS
disk image** below.

The Windows zip and both macOS tarballs contain the executable and nothing
else. **The Linux tarball carries four more members** — `install-desktop.sh`,
`my-claude-code-desktop.desktop` and `icons/{512x512,256x256,128x128,32x32}.png`
— so the same archive is both what delivery path A fetches and what a Fedora
user extracts and installs. Path A is unaffected: it takes the one member whose
basename is `MyClaudeCode` and ignores the rest, which
`tests/config/test_desktop_shell.py` pins from the Python side and
`smoke/linux-tarball.sh` pins from the archive's.

**The names carry no version** (decision Q5). A Start Menu shortcut, a
`.desktop` `Exec=` line and the per-target table S4 pins in Python all name a
file that never changes, so an upgrade never orphans them. The release tag is
the version; the asset name is the platform.

Alongside them, one more asset:

```
SHA256SUMS-desktop-shell.txt
```

Seven lines — the four archives, the Windows installer, the Linux `.deb` and
the macOS `.dmg` — sorted by filename,
in exactly the format `sha256sum -c` reads —
64 hex characters, **two spaces**, the filename, and nothing else:

```
57883dc18219d223e80e04ad9cbae306e04e18d907678c788b265c560694f0f8  MyClaudeCode-linux-x86_64.tar.gz
088360a67dcd32ab00ed1eedb2427645980926b74afe0253738ceafea601768a  MyClaudeCode-macos-aarch64.tar.gz
648460de2d3df68292a7e83401d12345b24b734073fd1405b99aa3ccba953238  MyClaudeCode-macos-x86_64.tar.gz
b4d8255397bc8000278665c92fd6196035cc5c030554784c1b6eb37094eea478  MyClaudeCode-windows-x86_64.zip
7d0d5b1fa9c2f0c1e9a6d3b7c2e1f4a8b6c9d2e5f8a1b4c7d0e3f6a9b2c5d8e1  MyClaudeCode-Setup-windows-x86_64.exe
3f9e1c7a5b2d8e4f0a6c3b9d5e2f8a1c4b7d0e3f6a9c2b5d8e1f4a7c0b3d6e9f  MyClaudeCode-linux-x86_64.deb
5c2a8f1d4b7e0a3c6f9b2d5e8a1c4f7b0d3e6a9c2f5b8d1e4a7c0f3b6d9e2a5c  MyClaudeCode-macos-universal.dmg
```

(The digests above are an illustration of the shape, not of any one release.)

Each leg rebuilds its line from the digest rather than printing whatever its
`sha256sum` felt like emitting, because the printers disagree: Git for
Windows' defaults to *binary* mode and writes `<digest> *<name>`, GNU
coreutils on Linux writes `<digest>  <name>`. Both are valid input to `-c`,
but they are not the same bytes, and a file whose format depends on which
runner produced which line is a contract nobody can parse. The aggregating job
asserts the shape of every line before uploading.

That format is a contract, not an accident. S4 fetches this file and parses it
into the `(platform, arch) -> (asset, sha256)` table that `config/rtk.py`
already establishes as the shape for a pinned, verified download; two spaces
and no leading `*` is what lets the same file serve both a human running
`sha256sum -c` and the Python side reading it.

Four runners cover four of the five targets `config/rtk.py` knows about.
`linux/aarch64` is deliberately absent — add an `ubuntu-22.04-arm` leg when
somebody asks for it.

Two dated notes, so the next person does not have to rediscover them:
`ubuntu-22.04` retires **17 April 2027** (it is used because glibc is
forward-compatible only, so the oldest supported base is the right place to
build; before that date, move the leg into an older-glibc container on
`ubuntu-latest`), and `macos-13` was retired **4 December 2025** — the Intel
runner is `macos-15-intel`.

### Why `cargo build`, not `cargo tauri build --no-bundle`

The workflow builds with

```
cargo build --release --locked --features custom-protocol --target <target>
```

The Tauri CLI's `build` does three things: run `beforeBuildCommand`, run cargo
with `--features custom-protocol`, and bundle. This project has no
`beforeBuildCommand` — `ui/` is one static HTML file and there is no frontend
build step — and `--no-bundle` switches the bundling off, so the CLI reduces to
exactly the line above, at the cost of compiling `tauri-cli` on four runners.

The feature is not optional. `tauri::is_dev()` is `!cfg!(feature =
"custom-protocol")`, and in a dev context the generated context reads
`frontendDist` off the disk at run time instead of embedding it — a binary
built without it would look for a `ui/` directory the user does not have. That
one flag is the whole difference between a release build and a development one,
which is why it is spelled out here rather than left to a CLI to remember.


## The Windows installer

`installer/windows/MyClaudeCode.iss` is an [Inno Setup 6](https://jrsoftware.org/)
script that wraps the Windows binary in a `setup.exe` for somebody who has never
heard of `uv`. The workflow compiles it on the Windows leg with Inno Setup
**6.7.3**, fetched from the project's own GitHub release and checked against a
pinned SHA-256 — deliberately not `choco install innosetup`, which follows the
channel and would have silently moved this build from Inno Setup 6 to 7 in
August 2026.

Build it locally the same way:

```
iscc /DAppVersion=6.45.0 ^
     /DSourceExe=..\..\src-tauri\target\release\MyClaudeCode.exe ^
     installer\windows\MyClaudeCode.iss
```

Both `/D` defines are optional — the script compiles without them, stamping
`0.0.0` and taking the binary from the default `target/release` path — so it can
be syntax-checked without inventing a release.

### What it does, and what it deliberately does not

| | |
| --- | --- |
| Installs | `MyClaudeCode.exe` and `app-icon.ico` into `%LOCALAPPDATA%\Programs\My Claude Code`, plus a Start Menu shortcut. A desktop icon is an unchecked task. |
| Privileges | `PrivilegesRequired=lowest`, so it is per-user, raises no UAC prompt, and its Apps & Features entry lands in `HKCU\...\Uninstall\{AppId}_is1` — the shape winget's `Scope: user` and `AppsAndFeaturesEntries.ProductCode` expect (spec S8). `PrivilegesRequiredOverridesAllowed=dialog` lets somebody who genuinely wants a machine-wide install ask for one. |
| Does **not** install | Python, `uv`, `mcc-server`, or any entry point. Decision Q4 moved the bootstrap into the application: on launch with no `mcc-desktop`, the window prints the exact install command and runs it, streaming the output (`src-tauri/src/install.rs`). An installer that also carried the server would be a second, divergent copy of `scripts/install.ps1` and would want administrator rights the moment it wanted to place a Python. |
| Does **not** write | The `HKCU\...\Run` autostart value. That value has exactly one writer — `_reconcile_start_at_login` in `cli/desktop.py`, from `desktop.json` — and a second one would make "remove the window" silently disable the server tray's autostart. A contract test asserts the `.iss` has no `[Registry]` section at all. |
| Signing | None (decision Q9). See below. |

### WebView2

The shell is a webview, so it needs the Microsoft Edge WebView2 runtime. The
script checks the registry key Microsoft documents for it — the `EdgeUpdate`
client `{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` and its `pv` version string, in
`HKLM` (32- and 64-bit views) and `HKCU`, treating absent, empty and `0.0.0.0`
alike as "not installed". Windows 11 ships the runtime as part of the OS and
Microsoft pushed it to Windows 10 from December 2022, so on nearly every machine
this finds it and nothing is downloaded.

When it really is missing, the ~2 MB **Evergreen Bootstrapper** is downloaded
from `https://go.microsoft.com/fwlink/p/?LinkId=2124703` and run with
`/silent /install`.

**Its SHA-256 is deliberately not pinned, and cannot be.** The bootstrapper is a
*rolling* download: its bytes change every time Microsoft ships a runtime, so a
pinned digest would turn the next runtime release into a broken installer for
everybody. What is pinnable is the **URL** — the permanent fwlink Microsoft
documents for exactly this purpose, over HTTPS to a Microsoft host — and that is
what the script pins. The alternative, the Fixed Version runtime, is 250+ MB and
would have to be hand-updated for every CVE.

A failed download is a warning, not an abort. A machine that is briefly offline
should still end up with the app installed; the runtime may arrive later through
Windows Update, and the window says what is wrong if it does not.

### Uninstall, and the split that matters

Inno records every `[Files]` and `[Icons]` entry in its own uninstall log and
removes them, so the executable, the icon and both shortcuts come back out
automatically; the contract test asserts that nothing carries
`uninsneveruninstall`. The `[UninstallDelete]` section adds the one thing the
log cannot know about — the WebView2 user-data directory the webview creates
next to the exe on first paint.

The app's remembered window size and position
(`%APPDATA%\com.myclaudecode.desktop`) is **asked about**, defaulting to *keep*.
That question lives in `[Code]` rather than `[UninstallDelete]` because
`[UninstallDelete]` entries are recorded at *install* time, so a `Check:` on one
would ask months before the answer matters. It uses `SuppressibleMsgBox`, so a
`/VERYSILENT` uninstall takes the default and never blocks.

**"Uninstall the desktop app" is not "uninstall My Claude Code."** The Apps &
Features entry is named `My Claude Code (desktop app)` so that this is visible
before anyone clicks it. It never touches `~/.local/bin`, `~/.mcc`, `~/.fcc`, or
the start-at-login value; removing those is `scripts/uninstall.ps1`, on explicit
consent. `tests/contracts/test_uninstaller_parity.py` asserts every part of
that: no `[Registry]` section, no entry naming a server path, no opt-out of
automatic removal, and a pinned `AppId`.

### Unattended installs

```
MyClaudeCode-Setup-windows-x86_64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

is the switch set winget supplies automatically for `InstallerType: inno`, which
is why `smoke/windows-installer.ps1` runs exactly it on every release build. An
installer that prompts under those switches makes every unattended install hang
forever, and that is not something to discover during winget validation.

The smoke also snapshots `HKCU\...\Uninstall`, `HKCU\...\Run` and the Start
Menu before the install and compares them after the uninstall: they must be
byte-identical. That is the machine-readable form of "removes everything it
created, and nothing it did not".

### winget

`installer/winget/` holds the three manifests for
`FiredMosquito831.MyClaudeCode` at manifest schema 1.28.0, plus `render.py`,
which produces them, and `SUBMIT.md`, which is the submission runbook.

**They are rendered, not written.** Six of the values in a winget manifest are
facts that live elsewhere in this repository and go stale silently: the
`ProductCode` is `AppId` from `MyClaudeCode.iss` with `_is1` appended;
`AppsAndFeaturesEntries`' `DisplayName`, `DisplayVersion` and `Publisher` are
the exact strings that installer writes into `HKCU\...\Uninstall`; `License` is
`pyproject.toml`'s; and `InstallerSha256` comes from the release's own
`SHA256SUMS-desktop-shell.txt`. `render.py` reads all six, and
`tests/scripts/test_winget_manifest.py` asserts that rendering the committed
version reproduces the committed bytes. So a change to the `AppId` — the one
change that would silently stop winget recognising an installed copy — is a red
test rather than a support thread.

    uv run --offline python desktop-shell/installer/winget/render.py v6.45.2

`winget validate --manifest desktop-shell/installer/winget/6.45.2` passes.

Three things are deliberately absent:

* **`InstallerSwitches`.** `InstallerType: inno` already means winget supplies
  `/SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`
  (`winget-cli`'s `GetDefaultKnownSwitches`), which is the set above. Writing it
  out here would be a second copy of a list Microsoft maintains, and the winget
  documentation asks you not to.
* **`Commands`.** The installer puts nothing on `PATH` — one windowed
  executable in `%LOCALAPPDATA%\Programs` and a Start Menu shortcut — so a
  `Commands: [MyClaudeCode]` entry would promise the user a shell command that
  does not exist.
* **A submission.** Nothing has been opened against `microsoft/winget-pkgs`.
  The package identifier is claimed permanently and is awkward to rename, so
  publishing is the maintainer's call; `SUBMIT.md` carries the exact steps, the
  pull-request text and the moderator checklist, with the evidence for each
  item already gathered.

On the identifier itself: the publisher segment is the **GitHub owner**, not a
company name. The community repository asks for "the name of the company that
publishes the tool" and there is no company; its own convention for that case is
the account name (`sharkdp.bat`, `ajeetdsouza.zoxide`). Note that this
deliberately differs from `AppsAndFeaturesEntries.Publisher`, which is
`My Claude Code`: that field is not a display name, it is what Inno writes into
the registry and what winget reads back to decide whether the package is
installed.

### Signing, and what SmartScreen actually shows

There is none (decision Q9), and the honest version is:

* The first person to run a given `MyClaudeCode-Setup-windows-x86_64.exe` sees
  SmartScreen's blue **"Windows protected your PC"** dialog. **More info → Run
  anyway** proceeds.
* That warning is about **reputation**, and reputation is **per file hash**. It
  accrues from real download volume, so it fades for a release many people
  install — and comes back for the next release, because that is a different
  file. There is no way to pre-warm it.
* **An EV certificate would not remove it.** Microsoft's guidance since 2024 is
  that EV no longer bypasses the SmartScreen prompt, so the several hundred
  dollars a year buys a publisher name in the dialog and not the absence of the
  dialog.
* On a machine with **Smart App Control** enabled, an unsigned installer is
  blocked outright with no "run anyway" at all. That is not a bug to work around;
  it is the point of the feature. The supported route there is the server
  one-liner, which downloads a wheel and verifies the SHA-256 GitHub publishes
  for it.

This is written down rather than worked around because the alternative — an
installer that claims to be signed, or a document that pretends the warning does
not happen — costs more trust than the warning does.

## The Linux packages

Two artefacts on the `ubuntu-22.04` leg, from one binary: the `.deb` for Ubuntu
and Debian, and the tarball — which now carries its own per-user installer — for
everything else.

### `MyClaudeCode-linux-x86_64.deb`

Built by `installer/linux/build-deb.sh`, which is a `dpkg-deb --build` and not
`cargo tauri build --bundles deb`. That is a deliberate choice, and the four
reasons are written at the top of the script: the Tauri bundler needs
`tauri-cli` (which this workflow deliberately does not compile on four
runners), names the file `My Claude Code_0.1.0_amd64.deb` (a space and a
version, both of which decision Q5 forbids in an asset name), takes its version
from `tauri.conf.json` rather than from the release tag, and appends its own
entries to `depends` — which is the one field this package exists to control.

The layout, in full:

| Path | Mode |
| --- | --- |
| `/usr/bin/MyClaudeCode` | 0755 |
| `/usr/share/applications/my-claude-code-desktop.desktop` | 0644 |
| `/usr/share/icons/hicolor/{512x512,256x256,128x128,32x32}/apps/my-claude-code-desktop.png` | 0644 |
| `/usr/share/doc/my-claude-code-desktop/copyright` | 0644 |
| `/usr/share/doc/my-claude-code-desktop/changelog.gz` | 0644 |

There is no 48x48 icon because there is no 48px source in this tree, and
resampling one in CI would put a slightly different image in every build.
hicolor scales between the sizes that are there.

The control fields that matter:

```
Package: my-claude-code-desktop
Architecture: amd64
Depends: libwebkit2gtk-4.1-0, libgtk-3-0t64 | libgtk-3-0, libayatana-appindicator3-1 | libappindicator3-1
```

Every alternative is load-bearing. Ubuntu 24.04 and Debian 13 renamed
`libgtk-3-0` to `libgtk-3-0t64` and `libappindicator3-1` to
`libayatana-appindicator3-1` during the 64-bit `time_t` transition, so naming
one spelling makes the package uninstallable on half the supported
distributions. The tray library is a **Depends** and not a Recommends (which is
what the spec's first draft said) because `apt install
--no-install-recommends` is common and a tray that silently is not there is
precisely the failure this arc exists to remove.

`postinst` and `postrm` do one thing each — `update-desktop-database` and
`gtk-update-icon-cache`, both `|| true` — so a machine without
`desktop-file-utils` still configures the package. Nothing else in either
script writes anything, and `tests/contracts/test_uninstaller_parity.py`
asserts that.

`lintian` runs over the package in CI and is **advisory**: its output is in
the log, with four suppressions for tags that are true of this package by
design (no man page for a GUI, no debhelper token in a hand-written maintainer
script, an unstripped binary -- release builds keep symbols so a crash report
is readable). It does not gate the release. It ships new tags with new
versions, and a policy checker turning a working package red on a Tuesday is
not a trade worth making when the actual gate -- install the package, run the
program out of it, remove it, diff the filesystem -- runs in the two steps
after it.

### The tarball's `install-desktop.sh`

POSIX `sh`, per-user, no root, and reversible:

```bash
tar -xzf MyClaudeCode-linux-x86_64.tar.gz
./install-desktop.sh
./install-desktop.sh --uninstall
```

It writes `~/.local/bin/MyClaudeCode`, a receipt beside it, an entry under
`~/.local/share/applications` and icons under `~/.local/share/icons/hicolor`,
and `--uninstall` removes exactly those. The entry's `Exec=` is rewritten to
the **absolute** installed path: a GUI launcher does not inherit a login
shell's `PATH`, and on Debian and Ubuntu `~/.profile` only adds
`~/.local/bin` to it when the directory already existed at login, so a relative
`Exec=` works from a terminal and fails from the menu.

### One icon, not two

The desktop app's entry is `my-claude-code-desktop.desktop`, `Name=My Claude
Code (desktop app)`, `Icon=my-claude-code-desktop`. The *server's* launcher —
written by `create_linux_desktop_entry` in `scripts/install.sh` — is
`my-claude-code.desktop`, `Name=My Claude Code`, `Icon=…/my-claude-code.png`.

Three separations, because each one alone is escapable:

1. **different filenames**, so neither installer overwrites the other's file;
2. **different `Name=`**, so a machine that ends up with both shows which is
   which rather than two identical tiles;
3. **different icon names** — a per-user icon *shadows* a system one of the
   same name, so sharing `my-claude-code` would mean the `.deb`'s icon silently
   never rendering.

And the common case never gets there at all: `install.sh --desktop` checks for
the app's entry, in `/usr/share/applications` and in
`~/.local/share/applications`, and steps aside if it finds one. It steps aside
only — removing either entry belongs to `scripts/uninstall.sh`.

### Uninstall, three lanes

| Command | Removes |
| --- | --- |
| `sudo apt remove my-claude-code-desktop` | everything the `.deb` put under `/usr` |
| `./install-desktop.sh --uninstall` | everything the tarball put under `~/.local` |
| `scripts/uninstall.sh` | My Claude Code itself, **plus** the per-user desktop app (binary, both receipts, entry, icons). Never the `.deb`: it detects the package with `dpkg-query` and prints the `apt remove` line instead, because deleting a dpkg-owned file behind dpkg's back leaves its database claiming a file that is gone. |

### No AppImage, no `.rpm`

AppImage was rejected for v1 on the spec's evidence: Tauri's own documentation
puts the bundle at 70+ MB against ~3 MB here, its bundler has to binary-patch
hardcoded WebKit paths out of `libwebkit2gtk*.so`, and the recurring failure
mode across Tauri issues #12463, #6992, #15665 and #15781 is a *silent* one —
WebKitWebProcess aborts and no window appears. An `.rpm` is not in this release
either; Fedora 40+ gets the tarball, which is the same binary without a second
packaging format to keep green.


## The macOS disk image

`installer/macos/build-app.sh` and `installer/macos/build-dmg.sh` turn the two
macOS legs' binaries into one `MyClaudeCode-macos-universal.dmg` (spec S9,
decision **Q7 as revised on 2026-09-04**: the original answer was "no `.dmg` in
v1"; the revision is "ship one, unsigned, and document Gatekeeper honestly").

### One universal image, not two per-arch ones

`build-app.sh` accepts `--binary` more than once and runs `lipo -create` over
what it is given, so the shipped `.app` carries both an `arm64` and an
`x86_64` slice. A person downloading a Mac app should not have to know which
processor their Mac has, and Apple's own answer to that question has been the
universal binary since 2020.

The cost is one extra CI job, because `lipo` cannot run until both macOS legs
have finished and a matrix leg has no way to wait for a sibling. The per-arch
**tarballs are still built and still uploaded**: delivery path A fetches
exactly one of them per machine and would gain nothing from downloading an
architecture it cannot execute.

### The bundle

| Path | Mode | Written by |
| --- | --- | --- |
| `My Claude Code.app/Contents/Info.plist` | 0644 | `build-app.sh` |
| `My Claude Code.app/Contents/PkgInfo` | 0644 | `build-app.sh` (`APPL????`, eight bytes, no newline) |
| `My Claude Code.app/Contents/MacOS/MyClaudeCode` | 0755 | `lipo -create` |
| `My Claude Code.app/Contents/Resources/MyClaudeCode.icns` | 0644 | `iconutil`, from `icons/icon.png` |
| `My Claude Code.app/Contents/_CodeSignature/CodeResources` | 0644 | `codesign` |

The icon is *generated* rather than copied from the checked-in `icon.icns`, so
it derives from the one source image the rest of this tree uses and cannot
drift from it: `sips -z` resamples the ten sizes `iconutil` wants into an
`.iconset`, `iconutil -c icns` folds them into one file. The fallbacks are
`sips -s format icns` (one representation, renders but does not scale as well)
and then the checked-in `icon.icns`, so a build never fails for want of an
icon. Which path was taken is printed, because "the icon looks slightly wrong"
is otherwise an unsearchable bug.

The `Info.plist` keys that are not decoration:

| Key | Why |
| --- | --- |
| `CFBundleIdentifier` = `com.myclaudecode.desktop` | The app's identity to macOS — **and** how `scripts/install.sh --desktop` recognises it and steps aside, and how `scripts/uninstall.sh` knows not to delete it. The launcher bundle `install.sh` writes carries `com.my-claude-code.desktop`; those two strings are the only thing separating two same-named applications at one path. |
| `CFBundleExecutable` = `MyClaudeCode` | Must equal `[[bin]] name` in `Cargo.toml`. |
| `CFBundleShortVersionString` / `CFBundleVersion` | The release tag without its `v`. **The bundle is versioned even though the executable is not** (decision Q5): Finder's Get Info shows this string, and an app that says `0.1.0` forever is an app nobody can report a bug against. Nothing points at a plist key, so there is no shortcut to orphan. |
| `LSMinimumSystemVersion` = `10.13` | Tauri v2's own bundler default for `minimumSystemVersion`; claiming lower would claim more than the framework does. The arm64 slice's real floor is 11.0, because Apple silicon did not exist before Big Sur — there is no plist key that can say that, and no need for one. |
| `NSHighResolutionCapable` | Without it macOS runs the window through the 1x magnifier and the whole dashboard is blurry on every Mac made since 2012. |
| `NSAppTransportSecurity` > `NSAllowsLocalNetworking` | The shell renders `http://127.0.0.1:<port>` and App Transport Security blocks cleartext HTTP by default. This is Apple's narrow exemption for loopback and `.local`; `NSAllowsArbitraryLoads`, which would exempt the whole internet, is deliberately absent and a contract test asserts it stays absent. |
| `LSApplicationCategoryType` | How Launchpad and Finder file it. |

### The image

`hdiutil create -volname "My Claude Code" -srcfolder <staging> -fs HFS+
-format UDZO -ov`, where the staging directory is the `.app` and a symlink to
`/Applications`. Two items and a symlink, and nothing else: a styled image
needs a window position baked into a `.DS_Store`, which means mounting
read-write and driving Finder over AppleScript — three flaky steps on a
headless runner, for an arrangement Finder overrides the first time anyone
opens the image anyway.

`-fs HFS+` is explicit because newer macOS versions have started defaulting
`hdiutil` to APFS, which older systems cannot mount at all; an image the app's
own `LSMinimumSystemVersion` could not open would make that claim false. The
`.app` is copied in with `ditto` and not `cp -R`, because a code signature
lives partly in extended attributes and only `ditto` preserves them — a broken
seal presents to the user as "the application is damaged", which no log
explains.

### Ad-hoc signed, and what that does and does not buy

`codesign --force --deep --sign - --timestamp=none` seals the bundle. That is
mandatory rather than decorative: on Apple silicon every Mach-O must carry at
least an ad-hoc signature, and `lipo` produces a fresh file whose inherited
signature was computed over something else. `--timestamp=none` because a
trusted timestamp needs Apple's timestamp server and is meaningless without an
identity to attach it to.

**Ad-hoc is a seal, not an identity.** There is no Developer ID behind it,
nothing for Gatekeeper to attribute the app to, and `spctl --assess` rejects
it. The honest version, which the README, `docs/USAGE.md` and every release
page say in the same words:

* a copy of the `.dmg` downloaded in a browser carries `com.apple.quarantine`,
  and Gatekeeper refuses to launch a quarantined app with no Developer ID;
* **the Control-click → Open bypass is gone** — Apple removed it in Sequoia,
  and on current macOS the dialog offers little more than *Move to Trash*;
* the one-time fix, after dragging the app across, is one Terminal command:
  `xattr -d com.apple.quarantine "/Applications/My Claude Code.app"`;
* the friction-free macOS route is therefore still the install script plus
  `mcc-desktop` (delivery path A), which fetches the same binary over HTTPS
  from Python — quarantine is applied by browsers, so a file Python downloads
  never has it and Gatekeeper is never involved;
* notarisation needs a Developer ID certificate, which needs a paid Apple
  Developer account at $99/year. This project does not have one, and an app
  claiming a signature it does not have would cost more trust than the warning
  does.

`smoke/macos-dmg.sh` **asserts the rejection**. `spctl --assess --type execute`
failing is what the paragraph above rests on, and a smoke that tolerated a
green `spctl` would be a smoke that never noticed the day that documentation
stopped being true. It also asserts there is no signing authority at all, so
the day somebody quietly adds a certificate, the docs get updated with it
rather than after it.

### One name, two applications

`scripts/install.sh --desktop` writes a launcher bundle at
`~/Applications/My Claude Code.app` whose executable simply execs
`mcc-desktop`. The `.dmg`'s application has the same display name, and a user
may drag it into `~/Applications` rather than `/Applications` — the same path.
In `/Applications` a second bundle would be a duplicate; in `~/Applications` it
would be an *overwrite*, a working application replaced by a shell wrapper.

Both scripts therefore read the `CFBundleIdentifier` and never the name:

| | |
| --- | --- |
| `install.sh --desktop` | Checks `/Applications` and `~/Applications` for a bundle carrying `com.myclaudecode.desktop` and prints "not adding a second launcher" if it finds one — the same sentence, and the same behaviour, as `create_linux_desktop_entry` stepping aside for the `.deb`'s menu entry. |
| `scripts/uninstall.sh` | Removes `~/Applications/My Claude Code.app` **only** when it is not the app, and prints the app's path, its contents and "drag it to the Trash" when it is. A bundle with no readable `Info.plist` is treated as the launcher, because `build-app.sh` always writes one. |

`tests/contracts/test_uninstaller_parity.py` pins both identifiers, pins that
they differ, parses `build-app.sh`'s bundle layout and asserts the
uninstaller's message covers every part of it.
