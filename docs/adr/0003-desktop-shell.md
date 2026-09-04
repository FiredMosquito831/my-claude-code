# ADR 0003 — The desktop shell, and how it reaches a user's machine

Status: accepted
Date: 2026-09-04

**Supersedes the *Alternatives rejected* section of [ADR 0002](0002-desktop-window-provider.md)
on the question of a compiled window.** Everything ADR 0002 decided about
*browser* providers still stands: Chromium app-mode remains in the chain, remains
the fallback, and remains what an explicit `--window app-mode` selects.

## Context

ADR 0002 gave `mcc-desktop` a window by launching a browser in app-mode. That
was the right call at the time and it works, but it is not an application: no
icon of its own, no dock entry, a second browser process with a private profile —
and on Linux, nothing at all, because `headless_refusal_reason()` refused
outright with "no Linux tray backend is packaged". `pystray` is declared
`sys_platform == 'win32' or sys_platform == 'darwin'` in `pyproject.toml` and
always will be, so Linux was never going to get a tray from Python.

ADR 0002 considered a compiled webview shell and rejected it on three grounds.
Two of them have to be answered rather than skipped:

| ADR 0002's objection | Status in 6.44.0 |
| --- | --- |
| "needs a Rust toolchain and a three-OS build matrix, which means a second release pipeline next to the wheel we already publish" | **True, and accepted deliberately.** `.github/workflows/shell-release.yml` builds on four runners and attaches the archives to the release the wheel is already on. It cannot break the wheel: `_select_wheel_asset` returns the first asset whose name ends `.whl` and cannot see an archive, which `tests/application/test_release_updates_ignores_shell_assets.py` pins. A red shell leg leaves a good release. |
| "`uv tool install` cannot deliver a compiled binary, so our entire installation story would have to change" | **No longer true, and it was already not true when ADR 0002 was written.** `config/rtk.py` has shipped a pinned third-party Rust binary for five targets, with a digest table, a download, a verify, an install into `~/.local/bin` and tests, since before 6.40.0. `config/desktop_shell.py` is that machinery pointed at our own release. The installation story does not change at all. |
| "on Linux it is *still WebKitGTK*, so it does not even buy the cross-platform uniformity that would justify the cost" | **True, and it does not matter.** Uniformity was never what was being bought — the dashboard already runs in three engines through app-mode. What is being bought is a tray on Linux, an icon in the dock, and a window that belongs to My Claude Code. WebKitGTK is the price of not shipping Chromium, and it is the right price. Recorded here as a known limitation rather than argued away. |

## Decision

1. **Tauri v2** for the shell (2.7–2.9 MB measured, OS webview, built-in tray and
   single-instance plugin, and an updater that is a plugin we simply never add).
2. **The shell is fetched, not bundled and not installed by an installer** —
   delivery path A. `mcc-desktop` downloads the pinned per-platform archive on
   launch, verifies it, and installs it. Installers (path B) come later and wrap
   this rather than replacing it.
3. **The digest is pinned in two places and both must agree.** The wheel carries
   the SHA-256 per target; the release carries `SHA256SUMS-desktop-shell.txt`.
   The fetch reads the published file first and refuses if it disagrees with the
   pin. A source-only pin gives no signal when a release asset is replaced; a
   file-only check trusts whoever can write to the release. Requiring both means
   changing what runs on a user's machine requires a reviewed commit.
4. **The pin moves in its own commit, after the fact.** The digests for a release
   do not exist until its shell build has finished, so a release cannot pin
   itself. `docs/RELEASE-CHECKLIST.md` §8 is the procedure.
5. **A failed fetch is never an outage.** Offline, proxied, an architecture with
   no build, a read-only home: each is a one-line warning and a fall-through to
   app-mode. The desktop app not launching would be far worse than the desktop
   app launching in a browser window.
6. **`auto` gains a first link; an explicit pin does not.** The chain `auto`
   walks is `shell → app-mode → pywebview → browser`. A pinned preference
   degrades through the browser providers only — `--window app-mode` means "not
   the shell", and is one of the two documented opt-outs alongside
   `DESKTOP_SHELL=off`.
7. **One tray icon. In this release it is Python's.** On Windows and macOS the
   pystray tray keeps the status area and the shell's tray is switched off
   through the shell child's environment; the tray's *Open Admin* raises the
   existing shell window by launching it again, which its single-instance guard
   answers. On Linux there is no Python tray and the shell's is the only one. The
   shell's tray takes over everywhere in a later release, at which point the
   Python tray retires (decision Q2).
8. **Unsigned, on both platforms, documented honestly.** Windows SmartScreen
   reputation is per file hash and even an EV certificate no longer bypasses the
   prompt; Smart App Control may block outright, and `install.ps1` plus app-mode
   remain the supported path there. On macOS the shell is unsigned too, but a
   file `urllib` downloaded is not quarantined — quarantine is applied by
   browsers — so path A never meets Gatekeeper's download gate. That asymmetry is
   the reason there is no `.dmg` (decision Q7): a browser-downloaded unsigned
   `.dmg` on current macOS can offer the user nothing but "Move to Trash".

   > **Q7 was revised later the same day, and this record is left standing so
   > the reversal is visible.** The revised answer is *ship one, unsigned, and
   > document Gatekeeper honestly*: `MyClaudeCode-macos-universal.dmg` is built
   > by the `macos-dmg` job and attached to every release. The reasoning above
   > was never wrong about the mechanism — a browser-downloaded unsigned `.dmg`
   > really is quarantined — it was wrong that the right response was to ship
   > nothing. `xattr -d com.apple.quarantine "/Applications/My Claude Code.app"`
   > is one line, and a person who wants to double-click something on a Mac is
   > better served by that line than by an absence. Path A (`mcc-desktop`
   > fetching the tarball with `urllib`) still avoids the gate entirely and is
   > still the recommended route. See `desktop-shell/README.md` §*The macOS disk
   > image*.

## Consequences

- Linux is supported for the first time, conditionally: a display *and* an
  installable shell. Without either, the refusal returns and now names why.
- `mcc-desktop --print-status` grows four additive keys (`shell_tray`,
  `shell_binary`, `shell_release_tag`, `shell_ready`) and `schema` stays at `1`.
- The server child is now spawned with `MCC_OPEN_BROWSER=0`. This fixes a real
  defect that predates the shell: `mcc-desktop` produced a window *and* a browser
  tab, because the server opens one on its own when it becomes healthy.
- A new failure surface exists that the repository has never had before — a
  network fetch on a GUI launch. It is bounded by being optional, cached behind a
  receipt, and non-fatal.
- What cannot be proven from here, and is not claimed: a real macOS or Linux
  launch, and first-launch SmartScreen/Gatekeeper behaviour.
