# ADR 0002 — Desktop window provider

Status: accepted
Date: 2026-08-19

## Context

`mcc-desktop` (ADR 0001) is a system-tray app. Its only way to show the admin
dashboard is `webbrowser.open()`, which drops a tab into whatever browser the
user has set as default, alongside every other tab they have open. There is no
window that belongs to My Claude Code.

Giving it a window is not one decision but a choice between engines, and the
obvious answer — "embed a native webview" — is three answers wearing one name:

- Windows → WebView2 (Chromium)
- macOS → WKWebView (Safari's engine)
- Linux → WebKitGTK

They are not interchangeable. Worse, `pywebview` on Linux needs
`gir1.2-webkit2gtk` and PyGObject installed as **system packages via apt**. Our
installers deliver a wheel with `uv tool install`; they cannot apt-install
anything, so on the one platform where the dependency is heaviest we cannot
guarantee it exists.

An audit of `admin_static/admin.js` found three behaviours the dashboard relies
on that embedded webviews break to varying degrees per engine:

| Dashboard feature | Call site | What breaks embedded |
|---|---|---|
| ChatGPT + Anthropic OAuth login | `admin.js:2302`, `admin.js:2364` — `window.open(url, "_blank", "noopener")` | Silently no-ops in several engines. **Login becomes unreachable.** |
| Analytics export (JSON/CSV/XLSX/TXT) | `admin.js:7649` — `downloadBlob` via `<a download>` | Downloads are disabled by default and engine-specific. |
| Copy buttons | `admin.js:3601`, `admin.js:7906` — `navigator.clipboard.writeText` | Availability varies by engine and by secure-context handling. |

One further constraint binds every option: `require_loopback_admin`
(`api/admin_routes.py:187`) checks both the TCP client address **and** the
`Origin` header. A window that loads `http://127.0.0.1:<port>/admin` over real
HTTP passes; anything presenting a `file://` origin is rejected with a 403.

## Decision

The window is a **seam with an ordered fallback chain**, not a single engine.
`cli/desktop_window.py` defines a `DesktopWindow` protocol
(`open` / `focus` / `close` / `is_open`) and three providers, tried in this
order by the `auto` preference:

1. **`AppModeWindow` — Chromium app-mode.** Launch the first available
   Chromium-family binary with `--app=<url>`, a private
   `--user-data-dir=<config_dir>/desktop-profile`, `--window-size=1400,900`,
   and `--class=MyClaudeCode` on Linux for taskbar grouping. Search order is
   Edge → Chrome → Brave on Windows (Edge ships with the OS), Chrome → Edge →
   Brave on macOS (Safari has no `--app` equivalent), and the usual six names
   on Linux. This is preferred because **it has zero functional regressions**:
   it is a real browser, so all three features above behave exactly as they do
   in the browser the dashboard was written against.
2. **`PywebviewWindow` — embedded webview.** Only when `import webview`
   succeeds. It must install shims before the page loads: `window.open` is
   bridged to the system browser through the JS API, downloads require
   `webview.settings["ALLOW_DOWNLOADS"]`, and the clipboard falls back to
   `document.execCommand('copy')`. If those preconditions are not met the
   provider reports itself unavailable rather than shipping a window where
   login silently fails.
3. **`BrowserTabWindow` — the current behaviour.** `webbrowser.open()`. Always
   available; `focus()` returns `False` because a tab we do not own cannot be
   raised.

The preference is persisted as `window` in `~/.mcc/desktop.json`
(`auto | app-mode | pywebview | browser`) and set with
`mcc-desktop --window <value>`. An explicit pin that turns out to be
unavailable **degrades through the rest of the chain with a logged warning**
rather than raising: a guardrail must degrade, not become an outage. An
unknown persisted value falls back to `auto` without crashing the app.

## Consequences

- `mcc-desktop` gets a real application window on machines with any
  Chromium-family browser, which in practice is every Windows machine (Edge)
  and most macOS and Linux desktops.
- We ship no new required runtime dependency. `pywebview` stays optional and
  is not installed by default, so in the delivered configuration the chain is
  effectively app-mode → browser tab.
- Chromium app-mode means a second browser process and a separate profile
  directory on disk. That profile is deliberately private, so the dashboard's
  session is not entangled with the user's daily browsing.
- We do not control the window's chrome, title bar, or menu. This is the price
  of not owning an engine, and it is the right price.
- `focus()` for app-mode is best effort: it re-invokes the same app command and
  relies on Chromium's profile process to raise the existing window.

## Alternatives rejected

- **`pywebview` first.** Rejected because of the three breakages above. The
  OAuth one is disqualifying on its own: an embedded window in which login
  silently does nothing is worse than a browser tab.
- **Tauri (or any Rust-based shell).** Rejected on three independent grounds.
  It needs a Rust toolchain and a three-OS build matrix, which means a second
  release pipeline next to the wheel we already publish; `uv tool install`
  cannot deliver a compiled binary, so our entire installation story would have
  to change; and on Linux it is *still WebKitGTK*, so it does not even buy the
  cross-platform uniformity that would justify the cost.
- **Electron.** Same packaging objection as Tauri, plus a ~150 MB runtime for a
  dashboard that already renders in a browser.
- **A single hard-coded engine with no chain.** Cannot express "this machine
  has no Chromium" or "this user prefers a plain tab", and turns a missing
  dependency into a dead app instead of a degraded one.
- **Keep `webbrowser.open()` only.** This is what we have; it is retained as
  the last link of the chain rather than as the whole strategy.
