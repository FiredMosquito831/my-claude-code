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

- **macOS** — Xcode command line tools. Untested: no macOS machine was
  available for this PR, and no `.dmg` ships in v1 (decision Q7).

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
`workflow_dispatch`. There is one release stream (decision Q6): the shell's
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

Each archive contains the executable and nothing else. The zip holds
`MyClaudeCode.exe`; each tarball holds `MyClaudeCode`, mode preserved.

**The names carry no version** (decision Q5). A Start Menu shortcut, a
`.desktop` `Exec=` line and the per-target table S4 pins in Python all name a
file that never changes, so an upgrade never orphans them. The release tag is
the version; the asset name is the platform.

Alongside them, one more asset:

```
SHA256SUMS-desktop-shell.txt
```

Four lines, sorted by filename, in exactly the format `sha256sum` emits and
`sha256sum -c` reads — the digest, **two spaces**, the filename:

```
3f...c1  MyClaudeCode-linux-x86_64.tar.gz
9a...7e  MyClaudeCode-macos-aarch64.tar.gz
...
```

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
