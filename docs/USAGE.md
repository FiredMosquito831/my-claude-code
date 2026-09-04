# My Claude Code — Complete Usage Guide

From a fresh machine to a tuned setup: installing, connecting Claude Code and Claude Desktop, adding providers, routing models, web search, and analytics.

The [README](../README.md) is the overview. This is the long-form manual.

---

## Contents

- [1. How it works](#1-how-it-works)
- [2. Install](#2-install)
- [3. First run](#3-first-run)
  - [Where your configuration lives](#where-your-configuration-lives)
  - [Legacy `~/.fcc`: migrating with `mcc-migrate`](#legacy-fcc-migrating-with-mcc-migrate)
  - [Pinning a directory with `MCC_CONFIG_DIR`](#pinning-a-directory-with-mcc_config_dir)
  - [Running the server with the desktop tray](#running-the-server-with-the-desktop-tray)
  - [Picking a window](#picking-a-window)
  - [Installing the desktop shortcut](#installing-the-desktop-shortcut)
  - [DESKTOP_* settings apply on the next launch](#desktop-settings-apply-on-the-next-launch)
  - [WSL and headless: there is no tray](#wsl-and-headless-there-is-no-tray)
  - [The embedded webview (pywebview), and its caveat](#the-embedded-webview-pywebview-and-its-caveat)
- [4. Tutorial: connect Claude Code (CLI)](#4-tutorial-connect-claude-code-cli)
- [5. Tutorial: connect Claude Desktop](#5-tutorial-connect-claude-desktop)
- [6. Tutorial: connect another CLI](#6-tutorial-connect-another-cli)
- [7. Providers and API keys](#7-providers-and-api-keys)
  - [Using Claude models](#using-claude-models)
  - [Custom providers](#custom-providers)
- [8. Model tiers and routing](#8-model-tiers-and-routing)
  - [Tiers for every other coding agent](#tiers-for-every-other-coding-agent)
  - [Tutorial: manage many models](#tutorial-manage-many-models)
- [9. Web search](#9-web-search)
- [10. Analytics](#10-analytics)
  - [Tutorial: read the request detail](#tutorial-read-the-request-detail)
  - [The Token Optimizer page](#the-token-optimizer-page)
- [11. Multi-key rotation](#11-multi-key-rotation)
  - [Tutorial: why my key was benched](#tutorial-why-my-key-was-benched)
  - [The RTK token optimizer](#the-rtk-token-optimizer)
- [12. Limits and resilience](#12-limits-and-resilience)
- [13. Updating](#13-updating)
  - [Stopping and restarting, and how long it takes](#stopping-and-restarting-and-how-long-it-takes)
- [14. Security and networking](#14-security-and-networking)
- [15. Troubleshooting](#15-troubleshooting)
- [Appendix: what changed in 6.x](#appendix-what-changed-in-6x)

---

## 1. How it works

My Claude Code is a **local server that speaks Anthropic's API**. Your coding agent believes it is talking to Anthropic. The proxy receives that request, forwards it to whichever provider you configured — NVIDIA NIM, OpenRouter, a local Ollama, 56 of them — and translates the response back into Anthropic's wire format.

<div align="center">
  <img src="../assets/how-it-works.svg" alt="Request flow from agent through the proxy to a provider" width="760">
</div>

Because the translation happens at the protocol level, streaming, tool use, reasoning blocks and image input keep working. Your agent doesn't know or care.

Three consequences worth internalising before you start:

1. **The server must be running.** It's a daemon, not a library. Close the terminal and your agent stops working.
2. **Your agent's model picker can list MCC's catalog**, not Anthropic's. Selecting "Sonnet" routes to whatever *you* mapped Sonnet to. Codex and Pi's pickers always do this; Claude Code's needs model discovery turned on (`mcc-claude --discover-models` or `mcc-claude-old`) — see [§4](#4-tutorial-connect-claude-code-cli).
3. **Credentials live server-side.** Your agent holds a token that only authenticates it to the proxy; the real provider keys never leave your machine.

<div align="center">
  <img src="../assets/pic.png" alt="Claude Code running through the My Claude Code proxy" width="720">
  <p><em>Claude Code, running normally, backed by a provider of your choosing.</em></p>
</div>

---

## 2. Install

> **Pick one environment and stay in it.** On Windows you can install under PowerShell *or* WSL. Both work — but they keep **separate configs** (`C:\Users\<you>\.mcc` versus `~/.mcc` inside WSL). Installing in both is the most common way to end up editing one config while the server reads the other.
>
> Already develop inside WSL? Install in WSL. Otherwise use PowerShell.

### Windows (PowerShell)

No admin rights needed:

```powershell
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts/install.ps1")))
```

If PowerShell blocks the script, allow it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### WSL, Linux, macOS

```bash
curl -fsSL "https://raw.githubusercontent.com/FiredMosquito831/my-claude-code/main/scripts/install.sh" | sh
```

### Then reopen your terminal

**This step catches almost everyone.** The installer appends `~/.local/bin` to your `PATH`, and an already-open shell will never see it. If `mcc-server` appears "not found" immediately after a successful install, this is why.

Verify:

```bash
mcc-server --version
```

### What the installer does — and doesn't

1. Installs `uv` (the Python tool runner) if missing or too old.
2. Looks up the **latest** release, downloads its wheel, and **verifies the SHA-256 that GitHub publishes for that asset**. A mismatch aborts rather than running unverified code.
3. Installs the package and puts `mcc-server`, `mcc-claude`, `mcc-claude-old`, `mcc-codex` and `mcc-pi` on your `PATH` (the legacy `fcc-*` spellings remain as aliases).

**It does not install Claude Code, Codex, or Pi.** Those are separate third-party tools and the proxy doesn't need any of them to run. Install whichever you actually use, yourself — the `mcc-*` launchers simply point an agent you already have at the proxy.

Pin a specific version instead of the newest:

```bash
sh install.sh --version 5.5.1      # PowerShell: -Version 5.5.1
```

Add `--dry-run` (`-DryRun`) to print what it would do without changing anything. Both scripts are readable before you run them: [install.sh](../scripts/install.sh), [install.ps1](../scripts/install.ps1).

---

## 3. First run

```bash
mcc-server
```

Keep this process running. Once healthy, the Admin UI opens in your browser automatically (disable with `FCC_OPEN_BROWSER=0`). The address is always printed in the startup log — by default:

```text
http://127.0.0.1:8082/admin
```

<div align="center">
  <img src="../assets/admin-page.png" alt="Admin dashboard overview" width="860">
</div>

The dashboard is where everything is configured. Every setting maps to a variable in `~/.mcc/.env`, and the UI writes to that same file — see [.env.example](../.env.example) for the fully annotated list. If you edit the file by hand, restart the server, because configuration is read at startup.

That file records **choices, not defaults**. A setting you have never touched appears as a commented placeholder naming what the code will use — `# FALLBACK_BENCH_ENABLED= (default: false)` — and a plain `KEY=value` line means it was set from the dashboard, so the field shows *set here* beside its label. Every field prints its default underneath, and one that was set gets a **Use default** button that removes the line again. Leaving a setting alone is what lets a later release change its default for you; storing the same value freezes it, which is the point of the distinction.

**Blank means unset.** Clearing a field removes its line, and the code default applies again — the one exception being a key your repo-level `.env` also sets, where MCC writes a bare `KEY=` to mask it if the setting's type accepts an empty value, and returns a warning naming the key if it does not. Warnings from a Save are shown in the dashboard rather than swallowed. Because "unset" is now a real state, selects carry an explicit **Default (…)** option and every boolean is a three-way choice — **Default (On)** / **On** / **Off** — instead of a checkbox that had no way to say "I never picked".

> **If you have been running MCC since before 6.1.0, check one thing.** Until then, the first Save of *anything* wrote every field's default into `~/.mcc/.env` as a real value — and a value on disk always outranks a code default, so no default could ever change for you again. The upgrade deliberately does not rewrite those lines (a value on disk is effective configuration). Open **Limits & Resilience** and look at **Bench failures** (`FALLBACK_BENCH_ENABLED`): if it shows a *set here* chip and you never chose the value, press **Use default**. This is the setting that has moved most — off in 5.58.0, on in 5.61.0, off again in 6.14.0 — and a line on disk has outlived every one of those. Separately, the next Save after 6.2.0 regroups the file under six section headings instead of one `# Limits` heading — the diff looks large, the values are untouched.

There is also a **Guide** tab inside the dashboard with a condensed version of this document, available offline.

On first run, the dashboard opens straight to a **Get Started** checklist instead of the Providers tab. It walks through configuring a provider, mapping model tiers, connecting Claude Code, and then points at the optional web search and analytics pages. Dismiss it once you're set up — the Get Started tab stays in the nav if you want it back.

### Where your configuration lives

Everything MCC keeps for you sits in one directory: the `.env` above, your
custom providers, the OAuth tokens under `auth/`, the per-agent documents the
`mcc-*` launchers write, and `logs/` (the server log plus the request-analytics
database).

| Install | Directory |
| --- | --- |
| A new install | `~/.mcc` — on Windows, `C:\Users\<you>\.mcc` |
| Installed before 6.40.0 | `~/.fcc`, the legacy directory, still used exactly as it always was |
| `MCC_CONFIG_DIR` set | whatever absolute path you gave it, override everything else |

The server prints which directory it chose on the first line of its startup
log, and the Get Started page repeats it. If you are ever unsure which config
you are editing, that is the authoritative answer.

The rule is short and it never moves anything:

1. `MCC_CONFIG_DIR` wins whenever it is set.
2. Otherwise `~/.mcc`, if it exists. If a legacy `~/.fcc` exists too, `~/.mcc`
   wins, the startup log names both, and the legacy one is left completely
   untouched — the two are never merged.
3. Otherwise the legacy `~/.fcc`, if it exists.
4. Otherwise a fresh `~/.mcc` is created.

**Nothing is ever migrated for you.** If you have been running MCC since before
6.40.0 your data stays in the legacy `~/.fcc` for as long as you like; the
directory is fully supported and there is no deadline. The *only* thing that
turns `~/.fcc` into `~/.mcc` is you running `mcc-migrate` yourself. There is no
dashboard button and no HTTP route for it, on purpose: relocating your keys and
your request history is not an action a web page you happen to have open should
be able to take.

#### Legacy `~/.fcc`: migrating with `mcc-migrate`

`mcc-migrate` (`fcc-migrate` is the same command) renames `~/.fcc` to `~/.mcc`
in a single atomic step. Nothing is copied, nothing is deleted, and nothing is
merged — it either relocates the whole tree at once or it refuses and leaves
everything as it was.

**Stop the server and the tray first.** This is not optional. A server that is
still running cached its log path at startup; it goes on writing to the old
location and recreates the legacy directory behind you, leaving you with two
half-configs. The command refuses to run while it can see a live MCC — it
checks the tray's lock file and knocks on the server's `/health` port — but
stop them yourself rather than relying on that.

```bash
# 1. Stop the server (Ctrl-C in its terminal) and quit the tray from its menu.
# 2. Then:
mcc-migrate
# 3. Start the server again:
mcc-server
```

Windows PowerShell is identical — the command is on your `PATH` after any
install.

What you should see:

```text
Moved C:\Users\you\.fcc to C:\Users\you\.mcc. Nothing was copied and nothing
was deleted.

Rollback note written to C:\Users\you\.fcc-old\RESTORE.txt.
```

The command creates one more directory, `~/.fcc-old`, containing a single
`RESTORE.txt` and no data at all. That file records the date and the exact
command to move everything back, written so that it **fails loudly** if the
legacy directory has reappeared instead of quietly nesting your config inside
it. Delete `~/.fcc-old` whenever you are satisfied; nothing reads it.

Reasons the command will refuse, all of which leave every file where it is:

| Message | What to do |
| --- | --- |
| `Refusing to migrate: … already exists` | Both directories are present. Only you know which holds the data you want — move the unwanted one aside, or just keep using `~/.mcc`. |
| `Refusing to migrate: an MCC server is answering …` | Stop the server, then re-run. |
| `Refusing to migrate: the desktop tray still holds …` | Quit the tray from its menu, then re-run. |
| `Could not move …: a file inside the legacy home is still open` | Windows only. The message lists the `mcc-*`/`fcc-*` processes holding files; close them and re-run. |

If you would rather not move anything, you do not have to. Setting
`MCC_CONFIG_DIR=C:\Users\you\.fcc` (or `~/.fcc`) pins the legacy directory
explicitly and silences the startup notice.

#### Pinning a directory with `MCC_CONFIG_DIR`

`MCC_CONFIG_DIR` is an environment variable, not a `.env` setting — the
directory has to be known before there is a `.env` to read. Set it in your
shell (or in the service definition that starts the server) to an absolute
path, and every process that reads it uses that directory and nothing else:

```bash
export MCC_CONFIG_DIR=/srv/mcc/config      # bash/zsh
$env:MCC_CONFIG_DIR = "D:\mcc\config"      # PowerShell, current session
```

It has to be set for the launchers too, not just the server — an `mcc-claude`
started in a shell without it reads a different directory than the server does.
There is no legacy FCC_-prefixed spelling of it; the variable was introduced
with the `MCC_` name and has only ever had that one.

#### Server logs under `logs/`

`logs/server.log` is the current log; older ones rotate to `server.<date>.log`.
`SERVER_LOG_RETAIN_FILES` (default `10`) is how many rotated files to keep —
the current one is never counted and never deleted. `0` keeps every rotated
file, which is how an earlier install grew a 17 GB `logs/` directory. The sweep
runs at startup as well as on each rotation, so lowering the number cleans up a
backlog on the next restart. `logs/requests.db` is the request-analytics
database and is never touched by log rotation.

### The two addresses that matter

| What | Default | Who uses it |
| --- | --- | --- |
| **Proxy API** | `http://127.0.0.1:8082` | your coding agent |
| **Admin UI** | `http://127.0.0.1:8082/admin` | you, in a browser |

Same port. The Admin UI is additionally restricted to loopback callers — see [Security and networking](#14-security-and-networking).

### Running the server with the desktop tray

Two optional commands change how the server is *owned*, and both write to the same `~/.mcc/desktop.json` state file:

| Command | What it does |
| --- | --- |
| `mcc-server` | The headless server. Blocks forever, binds `:8082`. This is the canonical path on WSL / headless Linux / macOS server. |
| `mcc-desktop` | A system-tray app. What it does on launch depends on its **Server mode** (below). |

The dashboard **Deployment** card (and the tray's **Server mode** menu) selects one of three modes:

| Mode | Meaning |
| --- | --- |
| `spawn` | The tray owns `mcc-server` as a child process. On launch, if nothing is listening on `:8082`, it starts the server itself. Best for Windows/macOS desktop users. |
| `attach` | The tray connects to an existing server on `:8082` and **never spawns one**. For people who run `mcc-server` themselves (WSL / headless / ssh). If nothing is listening, the tray reports "server not running" and offers to open the dashboard. |
| `off` | Tray only; the desktop app does not touch the server at all. |

The older boolean `server_auto_start` is migrated on first read: `true` becomes `spawn`, `false` becomes `attach`.

#### Start at login, per platform

**Start at Login** registers a different target depending on where the machine lives, and the dashboard shows only the option that applies to the detected platform:

| Platform | What is registered |
| --- | --- |
| Windows | HKCU `...\CurrentVersion\Run` entry for `mcc-desktop` (the tray). |
| macOS | A LaunchAgent for `mcc-desktop` (the tray). |
| WSL / Linux | A `systemd --user` unit (`~/.config/systemd/user/mcc-server.service`) for headless `mcc-server`, falling back to `~/.config/autostart/mcc-server.desktop` when systemd isn't available. |

You can also drive these from the command line:

```bash
mcc-desktop --server-mode spawn|attach|off
mcc-desktop --autostart on|off
mcc-desktop --status
```

<a id="picking-a-window"></a>

### Picking a window

There is no single "native window" API that behaves the same across Windows, macOS, and Linux (WebView2 / WKWebView / WebKitGTK all differ), so `mcc-desktop` resolves its window through a **provider chain** that prefers Chromium **app-mode**: a real browser process launched with no tabs and no URL bar, its own taskbar entry, and a private profile under `~/.mcc/desktop-profile`.

This is not a cosmetic preference. Three things the dashboard depends on **break inside an embedded webview**: `window.open` (both OAuth logins use it), `<a download>` (the analytics export), and `navigator.clipboard` (every copy button). App-mode is a real browser process, so all three keep working.

Choose it with `--window`:

```bash
mcc-desktop --window auto|app-mode|pywebview|browser
```

`auto` is the default — it tries app-mode first, then falls back to a plain browser tab if no Chromium-family browser (Edge, Chrome, Brave) is found. `mcc-desktop --status` reports which provider is currently in effect. Picking an option that isn't available on this machine falls back with a warning, not a failure.

The same choice is on the dashboard's Deployment card as a **Window** control, with a line underneath showing what `auto` currently resolves to (for example `auto → app-mode (Microsoft Edge)`). Reading this at launch means a change applies to the **next** `mcc-desktop` start, not a window already open.

Launching `mcc-desktop` a second time raises the existing window instead of opening a duplicate; closing the window does **not** stop the server (close ≠ quit) — use the tray menu or `--server-mode off` for that.

<a id="installing-the-desktop-shortcut"></a>

### Installing the desktop shortcut

Pass `--desktop` to the installer (`-Desktop` on PowerShell) to add a platform shortcut at install time:

```bash
curl -fsSL <install-script-url> | sh -s -- --desktop
```

```powershell
.\install.ps1 -Desktop
```

This writes a Start Menu `.lnk` on Windows, a `.desktop` entry on Linux, and a minimal `.app` bundle on macOS. It's opt-in — a plain install is unchanged — and if the shortcut can't be created, the installer warns and continues rather than failing the whole install.

<a id="what-the-uninstaller-removes"></a>

### What the uninstaller removes

`scripts/uninstall.sh` and `scripts/uninstall.ps1` remove everything the installers and the tray create, not just the command shims. Until 6.41.3 they removed the shims and the config directory only, which left a Start Menu entry pointing at a deleted `mcc-desktop.exe` and an autostart registration relaunching a package that was no longer installed.

**Removed:**

| Artefact | Path or key | Created by |
| --- | --- | --- |
| Command shims | the uv tool bin directory | `uv tool install` |
| Config, logs, data and the exported icon | `~/.mcc/` and a legacy `~/.fcc/` | normal use |
| Start Menu shortcut | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\My Claude Code.lnk` | `install.ps1 -Desktop` |
| Start-at-login value | `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run\MyClaudeCodeDesktop` | **Start at Login** |
| Desktop entry and icon | `~/.local/share/applications/my-claude-code.desktop`, `~/.local/share/icons/hicolor/256x256/apps/my-claude-code.png` | `install.sh --desktop` |
| App bundle | `~/Applications/My Claude Code.app` | `install.sh --desktop` |
| LaunchAgent | `~/Library/LaunchAgents/com.myclaudecode.tray.plist` | **Start at Login** (macOS) |
| systemd user unit | `~/.config/systemd/user/mcc-server.service` — `systemctl --user disable --now` runs first | **Start at Login** (Linux/WSL) |
| XDG autostart entry | `~/.config/autostart/mcc-server.desktop` | **Start at Login** (Linux/WSL, no systemd) |

**Kept:** uv, the uv-managed Python runtime, Claude Code, Codex, Pi, shared `PATH` entries, the shared XDG directories the entry and icon lived in, and the retired `~/.fcc-old/` (the legacy directory holding your rollback note). `~/.claude/` is never touched.

Ordering and safety are unchanged: the desktop artefacts are removed only **after** every shim is verified gone, so a failed or unverified tool removal leaves your config *and* your shortcut alone. A shortcut or registry value that cannot be deleted (a file the shell has open, a locked key) is reported as a warning rather than aborting an uninstall that has already removed the tool. `--dry-run` / `-DryRun` prints every removal without performing it.

<a id="desktop-settings-apply-on-the-next-launch"></a>

### DESKTOP_* settings apply on the next launch

> **These settings apply on the next `mcc-desktop` launch, not to a tray already running.** `mcc-desktop` is a separate process from `mcc-server` and reads them once at start — changing one in the dashboard or in `~/.mcc/.env` does nothing to a tray you already have open. Quit and relaunch `mcc-desktop` to pick it up.

Nine settings live under **Admin → Providers → Desktop**, beside the live desktop panel. They sat on the Limits page until 6.2.0; if you are following an older note, that is where they went.

| Setting | Default | Range |
| --- | --- | --- |
| `DESKTOP_HEALTH_POLL_SECONDS` | 5 | 0.5–3600 |
| `DESKTOP_HEALTH_FAILURE_THRESHOLD` | 3 | 1–1000 |
| `DESKTOP_ACTIVATION_POLL_SECONDS` | 1 | 0.1–3600 |
| `DESKTOP_SERVER_START_TIMEOUT` | 15 | 1–300 |
| `DESKTOP_ADMIN_REQUEST_TIMEOUT` | 5 | 0.5–60 |
| `DESKTOP_HEALTH_CHECK_INTERVAL` | 0.25 | 0.05–5 |
| `DESKTOP_WINDOW_WIDTH` | 1400 | 640–7680 |
| `DESKTOP_WINDOW_HEIGHT` | 900 | 480–4320 |
| `DESKTOP_BROWSER_PATH` | (empty) | any path |

`DESKTOP_BROWSER_PATH` points at a browser binary in a nonstandard location; if the path no longer exists, `mcc-desktop` warns and falls back to the built-in search instead of failing to start. `DESKTOP_WINDOW_WIDTH`/`HEIGHT` are only the window's *initial* size — once it has opened, its size and position are remembered across launches, so changing these later applies on first run or when you actually change the setting, not every launch.

<a id="wsl-and-headless-there-is-no-tray"></a>

### WSL and headless: there is no tray

`mcc-desktop` needs a desktop session — a tray, a window manager, a browser it can launch. WSL and headless Linux don't have one. Run `mcc-server` there instead; it's the same server without the tray, and it's the canonical path for WSL / headless Linux / macOS server (see [Running the server with the desktop tray](#running-the-server-with-the-desktop-tray) above). Trying to run `mcc-desktop` on WSL/headless now explains this and gives you the dashboard URL instead of hanging.

Reach the dashboard from a Windows browser at the address `mcc-server` prints on startup — normally `http://127.0.0.1:8082/admin`, which WSL forwards to Windows automatically in most configurations.

<a id="the-embedded-webview-pywebview-and-its-caveat"></a>

### The embedded webview (pywebview), and its caveat

A fourth window provider, `pywebview`, exists but **ships switched off** and is **not installed as a dependency**. Two reasons: MCC can't guarantee `pywebview`'s embedded webview handles downloads and external links correctly (the same `window.open` / `<a download>` / clipboard breakage described above), and on macOS its run loop conflicts with the tray's own run loop.

To opt in anyway: install `pywebview` yourself into the same environment MCC runs in, then set `--window pywebview` (or pick **Embedded webview** on the dashboard's Deployment card). It is present, gated, and unexercised by default — treat it as experimental, and expect OAuth login, the analytics export, and copy buttons to potentially misbehave inside it.

---

## 4. Tutorial: connect Claude Code (CLI)

Claude Code is configured through its **settings file**, not shell variables. This matters: `~/.claude/settings.json` takes precedence over exported environment variables, so `export ANTHROPIC_BASE_URL=...` in your shell will appear to do nothing if the settings file says otherwise.

### Step 1 — open the settings file

| Platform | Path |
| --- | --- |
| macOS / Linux / WSL | `~/.claude/settings.json` |
| Windows | `%USERPROFILE%\.claude\settings.json` |

If the file doesn't exist yet, create it.

Prefer not to hand-edit it? The **Claude Code settings file** card on the dashboard's
Providers view lists every settings file it can see on this machine (including the
Windows-side file when this server runs under WSL) and warns when a higher-precedence
file — like an enterprise managed settings file — already sets these variables and
would override the one you configure here.

### Step 2 — add the `env` block

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "freecc",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082"
  }
}
```

**Keep any other keys you already have** — merge these two entries into the existing `env` object rather than replacing the file.

- `ANTHROPIC_BASE_URL` points Claude Code at your local server.
- `ANTHROPIC_AUTH_TOKEN` is sent as a bearer token. It must match the proxy's own `ANTHROPIC_AUTH_TOKEN`, which ships as `freecc` in `.env.example`. If you changed it in the Admin UI, use your value here.

> **On the token:** it authenticates your agent *to the proxy*, nothing more. It is not a provider key. If you clear `ANTHROPIC_AUTH_TOKEN` on the server, the proxy stops requiring authentication altogether — convenient on a single-user machine, but read [Security and networking](#14-security-and-networking) first.

### Step 3 — restart Claude Code and verify

Restart the app, then run:

```text
/status
```

It should report:

```text
Anthropic base URL: http://127.0.0.1:8082
```

If it still shows Anthropic's own endpoint, the settings file wasn't picked up — check you edited the right path for your platform and that the JSON is valid.

### Step 4 — pick a model

No model overrides are needed — MCC exposes native **Fable / Opus / Sonnet / Haiku** tier models, so you can type a tier name at the `/model` prompt either way. Claude Code's built-in *picker*, though, only lists the MCC catalog once model discovery is on: add `"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"` to the `env` block from Step 2, or use `mcc-claude --discover-models` from the Shortcut below.

<div align="center">
  <img src="../assets/cc-model-picker.png" alt="Claude Code model picker showing MCC gateway models" width="720">
  <p><em><code>/model</code> in Claude Code, listing the MCC catalog (model discovery on).</em></p>
</div>

### Shortcut

If you'd rather not edit the settings file, the bundled launcher sets the two
proxy variables for the session:

```bash
mcc-claude
```

`mcc-claude` only sets `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` — it
doesn't touch anything else, since `~/.claude/settings.json` (Step 2 above)
takes precedence over environment variables anyway. This also means its
native model picker stays empty by default; pass `--discover-models` to have
`mcc-claude` additionally set `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`
for the session (an extra request to the proxy on every launch, so it's
opt-in):

```bash
mcc-claude --discover-models
```

If you want the previous `mcc-claude` behavior — gateway model discovery
enabled, the auto-compact window set, telemetry/autoupdate disabled, and
inherited `ANTHROPIC_*` variables cleared — run `mcc-claude-old` instead.

The legacy `fcc-claude`, `fcc-claude-old`, `fcc-codex` and `fcc-pi` aliases
behave identically.

Official references: [Claude Code LLM gateway docs](https://code.claude.com/docs/en/llm-gateway-connect) · [settings.json reference](https://code.claude.com/docs/en/settings)

---

## 5. Tutorial: connect Claude Desktop

The desktop app has a **native gateway setting** — no file editing required. Its *Code* tab also honours the `~/.claude/settings.json` above, but the gateway configuration below is the supported path for the app itself.

Menu labels shift slightly between app versions; this is the currently documented route.

### Step 1 — enable Developer Mode

**Help → Troubleshooting → Enable Developer Mode**

The app restarts and gains a **Developer** menu.

> On older builds the path is **Settings → enable Developer mode**, which exposes **Settings → Developer** instead.

### Step 2 — open the inference settings

**Developer → Configure Third-Party Inference…**

<div align="center">
  <img src="../assets/claude-desktop-developer-menu.png" alt="Claude Desktop Developer menu with Configure Third-Party Inference highlighted" width="780">
</div>

### Step 3 — fill in the Connection section

| Field | Value |
| --- | --- |
| **Connection** | `Gateway` |
| **Gateway base URL** | `http://127.0.0.1:8082` |
| **Gateway API key** | `freecc` |
| **Gateway auth scheme** | `bearer` |
| **Credential kind** | `Static API key` |
| **Model discovery** | on |

<div align="center">
  <img src="../assets/claude-desktop-gateway-config.png" alt="Claude Desktop third-party inference settings filled in for My Claude Code" width="780">
</div>

Then click **Apply Changes**.

Use the port from your server's startup log if it isn't `8082`, and match the API key to your `ANTHROPIC_AUTH_TOKEN` if you changed it from `freecc`.

### Step 4 — test before restarting

The dialog has **Test connection** and **Test model discovery**. Both hit your running MCC server, so use them to confirm the setup *before* restarting — **the server must be running** or they will fail.

### Step 5 — restart the app

With **Model discovery** on, the app populates its picker from MCC's `/v1/models` at launch, so you can leave **Model list** empty.

**Two things to expect:**

- The **initial warning dialog can be safely ignored.** The picker fills in once discovery completes.
- With a gateway active, the desktop app runs **local sessions only** — no Anthropic-hosted cloud environments.

---

## 6. Tutorial: connect another CLI

> **A coding agent is not a provider.**
> The CLIs in this section sit **downstream** of MCC: they send requests to it.
> The names on the Providers page — including `opencode`, `commandcode`,
> `cline`, `kimi_coding` and `kilo` — are **upstream** gateways MCC buys tokens
> from. Some names appear in both lists and mean different things, and both can
> be on at once: you can run a coding agent against MCC while the same-named
> upstream provider is switched off. In the code they are separate namespaces
> (`harness_id` in `cli/harnesses/` and `config/harnesses.py`, `provider_id` in
> `providers/` and `config/provider_catalog.py`) and are never joined.
>
> **Qwen Code is a third pair.** The providers `qwencloud` and
> `qwencloud_coding` are Alibaba endpoints MCC sends requests *to*, paid for
> with a DashScope key. `mcc-qwen` launches Alibaba's Qwen Code *CLI*, which
> sends its requests to MCC — and needs neither of those providers switched
> on. Its harness id is `qwen_code`, deliberately not `qwen`. `crush` collides
> with nothing today; the id is still spelled out in the registry so a future
> `crush` gateway cannot quietly take it over.
>
> **Cline is a pair too.** The provider `cline` on the Providers page is
> the Cline *gateway* MCC sends requests to. `mcc-cline` launches the Cline
> *CLI*, which sends its requests to MCC, and needs that provider switched
> on for nothing. Its harness id is `cline_cli`, deliberately not `cline`.
> `goose`, `aider` and `droid` collide with nothing today, and each id is
> spelled out in the registry so a future gateway of that name cannot
> quietly take it over.
>
> **Kimi Code is the pair most likely to be misread**, because the two halves
> do not even share a spelling. The providers `kimi` and `kimi_coding` are
> Moonshot endpoints MCC sends requests *to*, paid for with a Moonshot key.
> `mcc-kimi` launches Moonshot's Kimi Code *CLI* (`kimi`, published on PyPI as
> `kimi-cli`), which sends its requests to MCC — and needs no Moonshot account,
> no Moonshot key and neither of those providers switched on. Its harness id is
> `kimi_code`, deliberately not `kimi`.
>
> **Command Code is the sharpest case, because both halves ship.** The provider
> `commandcode` is the Command Code *gateway* MCC sends requests to, paid for
> with a `COMMANDCODE_API_KEY` you bought. `mcc-commandcode` launches the
> Command Code *CLI*, which sends its requests to MCC. Running the CLI routed
> to `anthropic` with the `commandcode` provider switched off is an ordinary
> setup. The registry even gives them different ids — the harness is
> `commandcode_cli` — so no lookup can resolve one and answer with the other.
>
> **Gemini is the newest pair, and the one where the word means three things.**
> The provider `gemini` on the Providers page is Google's own
> OpenAI-compatible endpoint MCC sends requests *to*, paid for with a Google
> AI Studio key. `mcc-gemini` launches Google's Gemini *CLI*, which sends its
> requests to MCC over the Gemini protocol — and needs that provider switched
> on for nothing. And `POST /v1beta/models/{model}:generateContent` is MCC's
> own *inbound* Gemini surface, which is what the CLI talks to. Three
> different things, one word: the harness id is `gemini_cli`, deliberately not
> `gemini`.

Every CLI MCC can launch is declared once, in `config/harnesses.py`. That one
declaration produces the `mcc-<id>` command, the `mcc-help` line, the
installer's verification list, the RTK toggles and the **Coding agents**
dashboard page — so an agent cannot be present in one of those and missing from
another.

Open **Coding agents** in the dashboard to see, per agent: whether its binary is
on your `PATH`, the command to copy, the protocol it will speak to MCC, the
catalogue MCC generates for it (with the file's path, when it was last written,
and how many models carry a value the CLI supplied rather than a provider), and
its RTK toggle.

MCC never installs a coding agent. When one is missing, its launcher prints that
agent's own install command and exits 127.

### Codex and Pi

Both have launchers that configure the environment for you:

```bash
mcc-codex      # Codex CLI against the local MCC Responses provider
mcc-pi         # Pi
```

(The legacy `fcc-codex` and `fcc-pi` aliases behave identically.)

Neither rewrites your own configuration. Codex is configured with ephemeral
`-c` assignments on the command line, and Pi is registered by a bundled
extension that lives only for that process.

Codex reads a model catalog that MCC generates, so its own picker works normally:

<div align="center">
  <img src="../assets/codex-model-picker.png" alt="Codex model picker with the generated MCC catalog" width="720">
</div>

<div align="center">
  <img src="../assets/codex.png" alt="Codex CLI running through My Claude Code" width="720">
</div>

### OpenCode, OpenCode 2 and Kilo

```bash
mcc-opencode    # OpenCode, against MCC's Anthropic Messages route
mcc-opencode2   # the OpenCode 2 preview (installs beside v1, binary opencode2)
mcc-kilo        # Kilo CLI, a fork of OpenCode with the same config schema
```

These three read their provider configuration from a **file**, which is the
first time an MCC launcher has needed something other than an ephemeral flag
list. MCC does not edit yours. Each CLI documents an environment variable that
names an *extra* config file, merged into its precedence chain rather than
replacing it — `OPENCODE_CONFIG` for OpenCode
([docs](https://opencode.ai/docs/config/)) and `KILO_CONFIG` for Kilo
([docs](https://kilo.ai/docs/code-with-ai/platforms/cli)) — so MCC writes a
document of its own under `~/.mcc` and hands the launched process its path:

| Command | File MCC owns | Variable it is handed with |
| --- | --- | --- |
| `mcc-opencode` | `~/.mcc/opencode-config.json` | `OPENCODE_CONFIG` |
| `mcc-opencode2` | `~/.mcc/opencode2-config.json` | `OPENCODE_CONFIG` |
| `mcc-kilo` | `~/.mcc/kilo-config.json` | `KILO_CONFIG` |

Your `~/.config/opencode/opencode.json` is never read, never written and never
backed up. Stop launching through MCC and the file you wrote is the file you
have.

**The server owns the file; the launcher reads it.** `mcc-server` writes every
agent's document under `~/.mcc` when it starts, and rewrites it whenever the
model inventory or any model's resolved capabilities change. `mcc-opencode`
opens the file it needs and launches — no HTTP, no wait. It asks the server to
build one only when the file is not there at all, which on a machine where the
server has ever run means never. See
[How an agent's model list gets to disk](#how-an-agents-model-list-gets-to-disk).

**Your proxy token is not in the file.** The generated document writes
`options.apiKey` as OpenCode's own `{env:MCC_OPENCODE_API_KEY}` substitution,
and the launcher sets that variable in the launched process only.

MCC appears inside these CLIs as the provider `mcc`, so its models are
addressed as `mcc/<provider>/<model>`:

```bash
mcc-opencode models mcc            # what MCC published, with real limits
mcc-opencode run "say ok"          # one prompt, non-interactively
mcc-opencode -m mcc/openrouter/anthropic/claude-sonnet-4.5
```

**OpenCode 2 caveat, measured.** v2 runs a background service that keeps the
configuration it started with, so a config MCC refreshed after that service
started does not reach it. Pass `--standalone` to run in a private server that
reads the current one:

```bash
mcc-opencode2 run --standalone "say ok"
```

Everything else you type is passed through unchanged; `opencode upgrade`,
`opencode uninstall` and `--version` reach the CLI without MCC configuring
anything or requiring a running proxy.

### Command Code

```bash
mcc-commandcode
```

Command Code is the one agent MCC serves by editing a file you own, because it
gives no alternative. Read out of the installed CLI's own bundle
(`command-code` 1.39.0, `dist/cli.mjs`), `getUserProvidersConfigPath` resolves
`$HOME/.commandcode/providers.json` — `USERPROFILE` only as a fallback — and
`loadProvidersConfig` reads that document and no other. There is no `--config`
path, no config environment variable, and no project-local file.

So MCC merges **one key**, `provider.mcc`, into it and treats everything else
as untouchable:

| Guarantee | How |
| --- | --- |
| One owner | Only `provider.mcc` is written. Every other key is read, carried through and written back byte-for-byte. |
| Backed up once | The document is copied to `providers.json.mcc-backup` before MCC's first edit, and that copy is never overwritten — so it is always your pre-MCC file, not yesterday's MCC output. |
| Idempotent | A refresh that resolves the same numbers writes nothing and does not churn the file's timestamp. |
| Never uninvited | The background refresh only touches the file when `provider.mcc` is already in it. Having a `providers.json` is not consent; running `mcc-commandcode` is. |
| Reversible | `mcc-commandcode --disconnect` deletes `provider.mcc` and leaves the rest of the document exactly as it was. |

**Your proxy token is not in the file.** Command Code refuses a literal key
there — "raw secrets don't belong in providers.json" — and expands a `"$VAR"`
reference from the process environment instead, so MCC writes
`"$MCC_COMMANDCODE_API_KEY"` and the launcher supplies the value to the child
process only. The `baseURL` beside it *is* written literally, because Command
Code validates that field with `new URL(...)` and substitutes nothing into it;
it is a loopback address on your own machine, not a credential.

MCC appears inside Command Code as the provider `mcc`:

```bash
mcc-commandcode --list-models          # what MCC published, with real limits
mcc-commandcode -p "say ok"            # one prompt, non-interactively
mcc-commandcode -m mcc/openrouter/anthropic/claude-sonnet-4.5 -p "say ok"
mcc-commandcode --disconnect           # remove MCC's key again
```

Maintenance subcommands — `update`, `login`, `logout`, `mcp`, `skills`,
`status`, `info`, `--version` — reach the CLI untouched and do not need a
running proxy.

**One thing MCC cannot do anything about, measured on 1.39.0.** Command Code's
headless `-p` mode checks you are signed in to *Command Code* before it runs a
turn, whatever model you asked for — `resolvePrintAuthentication` in its own
bundle — so `mcc-commandcode -p "…"` on a machine that has never run
`cmdc login` exits with `Error: Not authenticated`, even for an MCC-routed
model and even with `--local-only`. Sign in once, or set
`COMMAND_CODE_API_KEY`. MCC will not fake that credential for you: it belongs
to Command Code's account, not to this proxy.

### Kimi Code

```bash
mcc-kimi
```

Kimi Code is Moonshot's `kimi` CLI. It is a **Python tool on PyPI**, not an npm
package: install it with `uv tool install kimi-cli` or `pipx install kimi-cli`.
MCC never installs it — a missing binary prints Moonshot's own line and exits
127. The version everything below was read against is **1.50.0**, and it was
read out of the installed package's source rather than its docs, because four
things the docs would have told you are no longer true:

| What you might expect | What Kimi Code 1.50.0 actually does |
| --- | --- |
| `KIMI_CODE_HOME` overrides the config directory | There is no such variable. The share directory is `$KIMI_SHARE_DIR` or `~/.kimi`, and the config is `<share dir>/config.toml`. `~/.kimi-code/` is an older layout. |
| `[models."…"]` has `max_output_size` | It does not. `LLMModel` is `provider`, `model`, `max_context_size`, `capabilities`, `display_name` — nothing else. Kimi derives the completion cap itself from `max_context_size`. |
| `capabilities` names vision, tools and reasoning | The whole vocabulary is `image_in`, `video_in`, `thinking`, `always_thinking`. There is no tools capability and no per-model reasoning-effort field anywhere. |
| `KIMI_MODEL_*` / `KIMI_API_KEY` can override a provider | Only for provider types `kimi`, `openai_legacy` and `openai_responses`. An `anthropic` provider — the only type that reaches MCC — falls through untouched. |

**Your `~/.kimi/config.toml` is never read, written or backed up.** Kimi
publishes `--config-file PATH`, so MCC writes a `config.toml` of its own at
`~/.mcc/kimi-code-config.toml` and passes that path for the launch. The share
directory is deliberately *not* redirected: it also holds your sessions, your
credentials, your plugins and the background-worker state, and moving it to
serve one config file would hide every session you have.

The trade, stated rather than hidden: `--config-file` **replaces** the config
document, it does not overlay it. So an `mcc-kimi` session takes Kimi's own
defaults for `theme`, `hooks` and `loop_control` rather than yours. Sessions,
skills and MCP servers are unaffected — those come from the share directory and
from Kimi's own MCP defaults, neither of which MCC touches.

**This is the one agent whose generated file holds the proxy token**, and the
reason is in the table above: `api_key` is a plain string with no `"$VAR"`,
`"{env:VAR}"` or `"!command"` form, and Kimi's environment overrides do not
reach an `anthropic` provider. There is no out-of-band channel, so the choice
was a literal value or no Kimi Code support at all. The literal goes into a
file MCC owns under `~/.mcc`, mode `0600`, in the same directory as the
`~/.mcc/.env` that already holds the identical `ANTHROPIC_AUTH_TOKEN` in
clear — nothing is disclosed that was not disclosed already, and nothing is
written into a document you own. With proxy auth off, the value written is the
`fcc-no-auth` marker, which is not a credential.

MCC appears inside Kimi Code as the provider `mcc`, and its models as
`mcc/<provider>/<model>`. Kimi Code has **no model-list subcommand** and MCC
states no default model, so name one on the command line or pick it with
`/model` once the session is up:

```bash
mcc-kimi -m mcc/openrouter/anthropic/claude-sonnet-4.5
mcc-kimi --print -p "say ok"            # one prompt, non-interactively
mcc-kimi --quiet -p "say ok"            # only the final message
```

Maintenance subcommands — `login`, `logout`, `info`, `export`, `mcp`,
`plugin`, `vis` — and `--help` / `--version` reach the CLI untouched and do not
need a running proxy. `acp`, `term` and `web` are not passed through: they run
an agent, so they get MCC's provider like any other session.

### Qwen Code

```bash
mcc-qwen
```

Qwen Code is Alibaba's `qwen` CLI: `npm install -g @qwen-code/qwen-code`. MCC
never installs it — a missing binary prints Alibaba's own line and exits 127.
The version everything below was read against is **0.15.11**, out of the
installed package's `cli.js` and then confirmed on the wire, because two things
the survey expected turned out to be wrong in MCC's favour:

| What you might expect | What Qwen Code 0.15.11 actually does |
| --- | --- |
| `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL` is how you point it at a proxy | It works, but it is the *lowest*-precedence source. `loadCliConfig` resolves `argv.authType \|\| settings.security.auth.selectedType \|\| getAuthTypeFromEnv()`, so an auth type you once picked in Qwen's UI silently outranks the environment. |
| Selecting the `anthropic` auth type needs a `security.auth.selectedType` write | It does not. `--auth-type anthropic` is a real flag whose `choices` include it, and it outranks both the settings key and the environment. MCC writes nothing under `~/.qwen`. |
| The Anthropic route carries one model, from `ANTHROPIC_MODEL` | `settings.modelProviders.anthropic` is an **array** of `{id, name, baseUrl, envKey, generationConfig}` records — the shape Qwen's own provider wizard writes — so MCC publishes the whole ladder with real context windows. |

**Your `~/.qwen/settings.json` is never read for MCC's sake, never written and
never backed up.** Qwen publishes `QWEN_CODE_SYSTEM_SETTINGS_PATH`, so MCC
writes `~/.mcc/qwen-code-settings.json` and names it in the launched process's
environment only.

**The proxy token is not in that file.** `envKey` names an environment
variable and Qwen reads `process.env[envKey]` at request time, so the file
holds `"envKey": "MCC_QWEN_API_KEY"` and the launcher supplies the value.

The trade, stated rather than hidden: Qwen declares `modelProviders` a
**replace** key and the scope that variable selects is the highest-precedence
one, so for the length of an `mcc-qwen` session MCC's provider list is the
whole list — your own `modelProviders` entries are not merged in. Every other
setting you have deep-merges as usual, because the document MCC writes contains
nothing else.

MCC's models appear under their full gateway ids, which is what Qwen sends on
the wire. Qwen Code has **no model-list subcommand**; `/model` lists what MCC
wrote:

```bash
mcc-qwen "say ok"                                # one prompt, non-interactively
mcc-qwen -m anthropic/openrouter/gpt-5           # start on one model
mcc-qwen -o json "say ok"                        # machine-readable
```

Maintenance subcommands — `mcp`, `extensions`, `auth`, `hooks`, `channel` —
and `--help` / `--version` / `--list-extensions` reach the CLI untouched and do
not need a running proxy. `review` is not passed through: it runs an agent.

### Crush

```bash
mcc-crush
```

Crush is Charm's `crush` CLI: `npm install -g @charmland/crush`, or the Go
binary from its GitHub releases. MCC never installs it. The version everything
below was read against is **v0.92.0**, from its own published JSON schema
(`crush schema` — an undocumented command, not in `--help`) and then measured
on the wire, because the survey's picture of it was out of date:

| What you might expect | What Crush v0.92.0 actually does |
| --- | --- |
| `crush.json` is deprecated; `crushrc` (a Bash script) is the format | Both load, and `crush schema` still publishes the full JSON schema. If a directory has both they merge, with `crushrc` winning. |
| You configure a provider with `crush provider add …` | There is no `provider` command in `--help`. |
| There is no config override, so MCC must merge into your file | `CRUSH_GLOBAL_CONFIG` replaces the global config **directory**, so MCC owns `~/.mcc/crush/` and writes one `crush.json` into it. No merge, no backup file. |
| `discover_models` will find MCC's models | It asks `GET <base_url>/models` — not `/v1/models` — so it finds nothing against a root base URL, and the `/v1` base URL that would reach it breaks `POST /v1/messages`. MCC sets `discover_models: false`. |

**Your `~/.config/crush` is never read for a provider, written or backed up.**
Neither is your data directory: every session, log and statistic Crush has
stays exactly where it was, and only `crush dirs`' first line changes.

**The proxy token is not in that file.** Crush's schema gives
`"$OPENAI_API_KEY"` as the example for `providers.<id>.api_key`, so MCC writes
`"$MCC_CRUSH_API_KEY"` and the launcher sets that variable in the child process
only. Measured: the outgoing request carried the variable's value as
`x-api-key`.

The trade, stated rather than hidden: the variable moves the whole global
layer, so an `mcc-crush` session takes Crush's own defaults for the LSP
servers, MCP servers, permissions and theme you set *globally*. Project-local
configuration — `.crushrc`, `crushrc`, `.crush.json`, `crush.json` in or above
the working directory — is a separate layer and still applies.

**Crush is the harness where "unknown stays unknown" costs the most.** Ten of
its per-model fields are *required*, so a capability nobody published cannot be
omitted and becomes Crush's own value instead. Every one of those is listed in
the file's `_mcc_defaulted` block, on the launcher's stderr and on the Coding
agents card. Two of the numbers were measured rather than assumed: with
`default_max_tokens: 0` Crush's agent request goes out with `max_tokens: 4096`
while its title request goes out with `0`, so MCC writes 4096; `context_window:
0` loads, runs and reaches no request body, so it is written as is.

MCC appears inside Crush as the provider `mcc`, and its models as
`mcc/anthropic/<provider>/<model>`:

```bash
mcc-crush run "say ok"                  # one prompt, non-interactively
mcc-crush models                        # every model Crush can see
mcc-crush dirs                          # which config directory this launch uses
```

Maintenance subcommands — `completion`, `help`, `login`, `logout`, `logs`,
`projects`, `update-providers` — and `--help` / `--version` reach the CLI
untouched and do not need a running proxy. `run`, `models`, `session` and
`dirs` are not passed through: the first three need MCC's provider and the
last should report the directory this launch actually uses.

### Cline

```bash
mcc-cline
```

Cline is the `cline` CLI: `npm install -g cline`. MCC never installs it.
Everything below was read against **3.0.61** and then measured on the wire,
because the survey's picture of it was wrong in two ways:

| What you might expect | What Cline 3.0.61 actually does |
| --- | --- |
| Configure it by running `cline auth --provider openai-native …` against `~/.cline` | `cline --config <dir>` moves the whole configuration directory, and Cline derives its data directory from the settings file inside it. MCC owns `~/.mcc/cline/` and passes the flag. |
| `openai-native` is the OpenAI-compatible provider | It is OpenAI's own hosted entry. `openai` is an alias that normalises to `openai-compatible`, which is the one described as "OpenAI-compatible chat completions endpoint", takes an arbitrary `baseUrl`, and has no `modelsSourceUrl` — so it makes no discovery call to a route MCC does not serve under that id. |
| Writing the provider block is enough | It is not. With the block written but not selected, Cline fell back to its own hosted `cline` provider and failed with "Unauthorized … re-authenticate your Cline account". MCC passes `-P openai-compatible` on every session launch. |
| Leave `apiKey` out and export `OPENAI_API_KEY` | Cline does have that fallback, and on 3.0.61 it neither authenticated nor terminated — the run hung. With the key in the document the same run answered in 885 ms. |

**Your `~/.cline` is never read for a provider, written or backed up.** The
base URL is `<root>/v1`: `@ai-sdk/openai-compatible` appends `chat/completions`
and nothing else.

**This is one of two generated files that carries the proxy token literally.**
It is MCC's own file under `~/.mcc/cline/`, mode `0600`, beside the `.env` that
already holds the same value — the same treatment, and the same justification,
as Kimi Code's `config.toml`.

**Cline's schema carries limits for one model at a time.** There is no
per-model array in `providers.json`: the provider entry holds `contextWindow`
and `maxTokens` for the model it names. So MCC records every routable model's
resolved limits in an inert `_mcc_models` block and promotes the one named by
`-m` / `--model` into the provider block before the file reaches disk. Verified:
with that in place, Cline's own run result echoed back
`info: {contextWindow: 131072, …}` — the ladder's figure for that ref.

```bash
mcc-cline "say ok"                       # headless; -p is --plan, not --print
mcc-cline --json "say ok"                # the same run as JSON events
mcc-cline -m anthropic/<provider>/<model>
mcc-cline -i                             # Cline's interactive TUI
mcc-cline config                         # which configuration this launch uses
```

Maintenance subcommands — `doctor`, `plugin`, `skill`, `mcp`, `schedule`,
`hook`, `connect` — and `--help` / `--version` / `--update` reach the CLI
untouched and do not need a running proxy.

### Goose

```bash
mcc-goose
```

Goose is Block's `goose` binary, from its GitHub releases. MCC never installs
it. Read against **1.48.0**.

**Goose is the one agent MCC configures without writing a file anywhere.** That
is a decision, not a gap. Goose 1.48.0 does have a declared-model mechanism —
a JSON file under `<config dir>/custom_providers/` whose `models[]` carry
`context_limit` and per-token costs — but the config directory is Goose's own
(`%APPDATA%\Block\goose\config` on Windows, `~/.config/goose` elsewhere), it
holds the user's own settings, and Goose publishes no variable or flag moving
the config file alone. Writing there would put an MCC-owned document inside a
directory MCC does not own, which is the one thing every launcher here exists
to avoid.

Everything Goose needs comes from the environment, set in the launched process
only:

| Variable | Value | Why |
| --- | --- | --- |
| `OPENAI_HOST` | the proxy root | Goose joins host and path with RFC 3986 rules |
| `OPENAI_BASE_PATH` | `v1/chat/completions` | Goose's own default, no leading slash — the pair resolves to `<root>/v1/chat/completions` |
| `OPENAI_API_KEY` | your proxy token | sent as `Authorization: Bearer` |
| `GOOSE_PROVIDER` | `openai` | selects the provider with no `config.yaml` |
| `GOOSE_MODEL` | the model this session runs on | your own `--model` outranks it |
| `GOOSE_CONTEXT_LIMIT` | that model's resolved context window | the one place Goose accepts a resolved capability |
| `GOOSE_DISABLE_KEYRING` | `1` | so a locked or absent keyring never prompts for a credential this session does not use |

Measured with an empty config directory: Goose ran straight through and its own
`config.yaml` stayed "missing (can create)". Model discovery still works —
Goose's OpenAI provider fetches `<host>/v1/models` for its picker, which is a
route MCC serves, so `goose configure` lists exactly what `GET /v1/models`
publishes.

```bash
mcc-goose run -t "say ok" --no-session   # one prompt, nothing left behind
mcc-goose session                        # interactive
mcc-goose run --model anthropic/<provider>/<model> -t "say ok"
mcc-goose info -v                        # provider, model and context limit
mcc-goose configure                      # Goose's own wizard, listing MCC models
```

Maintenance subcommands — `update`, `completion`, `help`, `recipe`, `plugin`,
`local-models` — and `--help` / `--version` reach the CLI untouched.

### Aider

```bash
mcc-aider
```

Aider is the `aider` command from the `aider-chat` PyPI package:
`uv tool install aider-chat`. MCC never installs it. Read against **0.86.2**.

**Aider reads two model documents, and looks for each of them in the working
directory, the git root *and* your home directory** — so simply writing them
would mean writing into your `~`. Both have a flag, so MCC owns both files:

| Flag | MCC's file | What it carries |
| --- | --- | --- |
| `--model-metadata-file` | `~/.mcc/aider-model-metadata.json` | LiteLLM's `model_cost` schema: `max_input_tokens`, `max_output_tokens`, `max_tokens`, `input_cost_per_token`, `output_cost_per_token`, `supports_vision`, `litellm_provider`, `mode` |
| `--model-settings-file` | `~/.mcc/aider-model-settings.yml` | a list of `ModelSettings` records: `accepts_settings` (`reasoning_effort`, `thinking_tokens`) and `use_temperature` |

The split is not arbitrary. The first says *what the model is*; the second says
*what it accepts*, which is what decides whether `--reasoning-effort` is
honoured at all. The second is loaded with `ModelSettings(**entry)`, so an
unrecognised key raises — which is why the `_mcc_defaulted` record lives only
in the first, a plain `dict.update` that tolerates it. The settings file is
written as JSON, which `yaml.safe_load` parses identically, so there is one
atomic writer rather than two encoders to keep in step.

**The metadata file is merged over LiteLLM's own registry, not instead of it**,
and an exact hit short-circuits LiteLLM's price-table fetch from GitHub — so a
generated entry also stops Aider phoning out for a model it would never find
there.

**The proxy token is not on disk.** `OPENAI_BASE_URL` (LiteLLM's preferred
name) and `OPENAI_API_BASE` (its fallback) are both set to `<root>/v1`, and
`OPENAI_API_KEY` carries the token, all in the launched process only. The `/v1`
matters: LiteLLM's `get_complete_url` appends `chat/completions` and inserts no
version segment of its own.

Models appear as `openai/anthropic/<provider>/<model>`. The `openai/` prefix
selects LiteLLM's OpenAI handler and is stripped before the request body; the
metadata file is keyed by the whole prefixed string, because that is the exact
key `Model.get_model_info` looks up.

```bash
mcc-aider --message "say ok"             # one prompt, non-interactively
mcc-aider --model openai/anthropic/<provider>/<model>
mcc-aider --list-models openai/anthropic # every MCC-routed model Aider can see
mcc-aider --exit --verbose               # resolve everything, send nothing
```

### Droid

```bash
mcc-droid
```

Droid is Factory's `droid` binary, from `app.factory.ai/cli`. MCC never
installs it. Read against **0.210.0**.

**Droid does not use the chat-completions door, and that is the finding.** The
survey grouped it with the OpenAI-only CLIs and expected a
`generic-chat-completion-api` custom model. Measured, `provider: "anthropic"`
accepts an arbitrary `baseUrl`, instantiates the bundled `@anthropic-ai/sdk`
against it and reaches `POST <baseUrl>/v1/messages` — MCC's own native
protocol, with no translation between the agent and the router. The request log
row read `endpoint=/v1/messages protocol=anthropic status=success`. So that is
what MCC declares, and `baseUrl` is the proxy **root** with no `/v1`, because
the Anthropic SDK appends `/v1/messages` itself.

| What you might expect | What Droid 0.210.0 actually does |
| --- | --- |
| MCC must merge into `~/.factory/config.json` | `--settings <path>` is a runtime settings overlay, merged into the same hierarchy for that process only. MCC owns `~/.mcc/droid-settings.json`. (The persistent file is `settings.json` in current versions; `config.json` is a legacy snake_case fallback. MCC touches neither.) |
| A custom model needs a Factory login | It does not. With a fresh home and no login, `droid exec --model custom:…` logged "Invalid auth", classified the model `isByok`, made no `whoami` call and went straight to the custom base URL. |
| The `apiKey` must be a literal | Droid documents `${VAR}` and expands it from the process environment. MCC writes `${MCC_DROID_API_KEY}` and the launcher supplies the value. |

`authMode: "bearer"` switches the Anthropic SDK from its default `x-api-key` to
`Authorization: Bearer`; MCC accepts both, and Bearer is the header the rest of
this document names.

Models appear as `custom:anthropic/<provider>/<model>` — the `custom:` prefix
is part of the id you type, and not part of the `model` field in the document.

```bash
mcc-droid exec "say ok"                  # one prompt, non-interactively
mcc-droid exec --model custom:anthropic/<provider>/<model> "say ok"
mcc-droid exec --output-format json "say ok"
mcc-droid doctor                         # config, connectivity and auth state
```

Maintenance subcommands — `update`, `mcp`, `plugin`, `computer`, `doctor`,
`help`, `search` — and `--help` / `--version` reach the CLI untouched.

### Gemini CLI

```bash
mcc-gemini                                     # interactive
mcc-gemini -p "say ok"                         # one prompt, non-interactively
mcc-gemini -m anthropic/<provider>/<model> -p "say ok"
mcc-gemini -o json -p "say ok"                 # machine-readable
mcc-gemini --skip-trust -p "say ok"            # see the note on trusted folders
mcc-gemini mcp                                 # passed straight through
```

Gemini CLI is Google's `gemini`, and it is the only agent in this list that
speaks **neither** Anthropic Messages nor an OpenAI shape. It speaks Google's
own protocol and nothing else, which is why MCC now serves a third inbound
surface — `POST /v1beta/models/{model}:generateContent` — described under
[Connect a Gemini client](#connect-a-gemini-client) below.

**What `mcc-gemini` sets, and what it never touches.** Two environment
variables in the launched process only — `GOOGLE_GEMINI_BASE_URL` pointing at
this proxy and `GEMINI_API_KEY` carrying your `ANTHROPIC_AUTH_TOKEN` — plus one
MCC-owned settings document at `~/.mcc/gemini-cli-settings.json`, handed to the
CLI through its own `GEMINI_CLI_SYSTEM_SETTINGS_PATH` variable. **Your
`~/.gemini/settings.json` is never written and never read for authentication**,
and the OAuth tokens beside it are never read at all: the API-key path returns
before Gemini CLI's Code Assist client is ever constructed. Everything in your
own settings that MCC does not name — your theme, your MCP servers, your memory
settings — still applies, because Gemini CLI merges the *system* scope last and
MCC's document contains only three keys.

**Why a file at all.** Setting only the two variables does not work, and it
fails in a way that looks like a bug in MCC. Gemini CLI infers its auth type
from the environment, and the presence of `GOOGLE_GEMINI_BASE_URL` makes it
infer `gateway` — a value its own `validateAuthMethod` then refuses with
"Invalid auth method selected." before a single request leaves the machine. The
one settings key `security.auth.selectedType: "gemini-api-key"` short-circuits
that inference entirely. The same document also sets
`privacy.usageStatisticsEnabled: false`, because a session routed through a
local proxy has no business reporting itself to Google and there is no
environment variable for that switch.

**The model list.** Gemini CLI runs one model at a time and builds its `/model`
picker from a list of Google models compiled into the binary — MCC cannot add
entries to it. So the generated document sets `model.name` to your primary
routed model, `mcc-gemini` prints the full list of routable ids on startup, and
`-m <id>` reaches any of them. Each id also gets an entry under
`modelConfigs.customAliases`, which is where its output ceiling and reasoning
level land; that key merges with Gemini CLI's built-in presets rather than
replacing them.

**Trusted folders.** Gemini CLI refuses a headless run in a directory it has
not been told to trust, and answers with its own message naming `--skip-trust`
and `GEMINI_CLI_TRUST_WORKSPACE`. MCC does not set either for you: trusting a
working directory is your security decision, not a launcher's.

`mcp`, `extensions`, `budget`, `--help` and `--version` reach the CLI
untouched.

### Antigravity: checked, and not possible

Google's Antigravity CLI (`agy`) appears on the **Coding agents** page marked
**Not servable**, with the reason on the card. It is listed rather than omitted
so the question has a dated answer instead of being re-asked every quarter.

**Verified 2026-09-02 against `agy` 1.0.14.** Two independent blockers, either
of which alone is fatal:

* **Every credential path ends in a Google OAuth token.** The binary's auth
  chain is keyring, browser OAuth, Application Default Credentials and a
  corporate login — there is no API-key entry point, and the strings
  `GEMINI_API_KEY`, `GOOGLE_API_KEY` and `GOOGLE_GEMINI_BASE_URL` do not appear
  in it at all. Without a Google sign-in it stops at "You are currently not
  signed in."
* **It does not speak the public Gemini API.** It calls
  `/v1internal:generateContent`, `:loadCodeAssist`, `:onboardUser` and
  `:fetchAvailableModels` on `cloudcode-pa.googleapis.com` — the private Gemini
  Code Assist service — and never `/v1beta/models/{model}:generateContent`. Its
  model list is server-supplied, so `--model` cannot name an MCC route either.

`CLOUD_CODE_URL` does redirect the backend host, so a proxy in front of an
already-signed-in session is technically reachable. That would mean
reimplementing a private Google protocol and would still require your Google
account, which is a different product from the one this proxy is.

### Other CLIs that were checked and cannot be served

| CLI | Checked | Why not |
| --- | --- | --- |
| **Antigravity** (`agy` 1.0.14) | 2026-09-02 | Google OAuth only, and speaks the private Gemini Code Assist protocol rather than the public Gemini API. See above. |
| **Cursor CLI** | 2026-09-01 | No published base-URL or API-key override: the CLI authenticates against Cursor's own account service and routes every request through it. |
| **Amp** (`@sourcegraph/amp`) | 2026-09-01 | Server-side agent. The CLI is a thin client for Sourcegraph's hosted service and has no setting that moves the inference endpoint. |
| **Roo Code CLI** | 2026-09-01 | Ships as a VS Code extension host; its provider configuration lives in the editor's own storage with no file or variable a launcher can point at. |

If one of these publishes a base-URL override, the work is small — every
harness in this section is one entry in `config/harnesses.py` plus a launcher.

### What MCC tells an agent about a model

The catalogue MCC generates for an agent carries each model's **real**
metadata, as MCC's resolution ladder resolved it, translated into that CLI's
own schema:

| What the ladder resolves | Where it lands in Gemini CLI | Where it lands in Codex | Where it lands in Pi | Where it lands in OpenCode / Kilo | Where it lands in Command Code | Where it lands in Kimi Code | Where it lands in Qwen Code | Where it lands in Crush | Where it lands in Cline | Where it lands in Aider | Where it lands in Droid |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| context window | *(one model at a time; no field)* | `context_window` / `max_context_window` | `contextWindow` | `limit.context` | `contextWindow` | `max_context_size` | `contextWindowSize` | `context_window`  `contextWindow` | `max_input_tokens` | `maxContextLimit` |
| output ceiling | `generateContentConfig.maxOutputTokens` | *(Codex has no field)* | `maxTokens` | `limit.output` | `maxOutput` | *(Kimi has no field)* | *(Qwen has no field)* | `default_max_tokens`  `maxTokens` | `max_output_tokens` + `max_tokens` | `maxOutputTokens` |
| vision support | *(no field)* | `input_modalities` | `input` | `attachment` + `modalities.input` | *(no field)* | `capabilities: ["image_in"]` | `modalities.image` | `supports_attachments`  *(no field)* | `supports_vision` | `noImageSupport` |
| tool support | *(no field)* | `supports_parallel_tool_calls` | *(Pi has no field)* | `tool_call` | *(no field)* | *(Kimi has no field)* | *(Qwen has no field)* | *(Crush has no field)*  *(no field)* | `supports_function_calling` | *(no field)* |
| reasoning support | `thinkingConfig.thinkingBudget: 0` when known absent | `supports_reasoning_summaries` | `reasoning` | `reasoning` | `reasoning` | `capabilities: ["thinking"]`, plus `"always_thinking"` when it cannot be turned off | `reasoning: false` when known absent | `can_reason`  *(no field)* | *(via settings)* | `enableThinking` |
| reasoning efforts | `thinkingConfig.thinkingLevel`, one starting rung clamped to `LOW\|MEDIUM\|HIGH` | `supported_reasoning_levels`, clamped to Codex's own rungs | *(Pi has no field)* | `variants.<rung>`, clamped to OpenCode's own rungs | `reasoningEfforts[]`, clamped to `low\|medium\|high\|xhigh\|max` | *(Kimi has no field)* | `reasoning.effort`, one starting rung clamped to `low\|medium\|high` | `reasoning_levels[]` + `default_reasoning_effort`, clamped to `low\|medium\|high`  *(no field)* | `accepts_settings: [reasoning_effort]` | *(no field)* |
| prices | *(no field)* | *(Codex has no field)* | `cost.input` / `cost.output` | `cost.input` / `cost.output` | `cost.input` / `cost.output` | *(Kimi has no field)* | *(Qwen has no field)* | `cost_per_1m_in` / `cost_per_1m_out`  *(no field)* | `input_cost_per_token` / `output_cost_per_token` | *(no field)* |
| pinned parameters | *(inherited from the `chat-base` preset)* | *(Codex has no field)* | *(Pi has no field)* | `options` | `options` | *(Kimi has no field)* | *(Qwen has no field)* | `options`  *(no field)* | `use_temperature: false` | *(no field)* |

A model with a 32k window is advertised as 32k. A model that publishes only
`low` and `high` gets exactly those two rungs in Codex's picker — `xhigh`
disappears rather than being offered and rejected. A model that cannot reason
gets no effort list at all.

**Every one of those values walks the same ten-rung ladder.** The routed
deployment's own `/models` payload wins outright; only where it published
nothing does MCC consult models.dev — that provider's own bucket, then the
OpenRouter reference catalogue, then a guarded cross-provider vote — and the
rung that answered is recorded and shown beside the number on the **Models**
page. One resolver feeds the Models page, `/admin/api/catalogue-models`, every
generated catalogue and `GET /v1beta/models`, so those surfaces cannot
disagree about a number or about where it came from.

**Unknown stays unknown.** Where a CLI's schema makes a field optional and no
source anywhere published a value, MCC omits the key rather than writing a
zero. Where the schema *requires* a value, MCC uses **that CLI's own documented
default** — never a number MCC invented — and records the substitution. So you
can always tell which figures are the CLI's guess from which are your
provider's answer.

The record has three homes, and which ones you get depends on the CLI:

* a `_mcc_defaulted` block in the generated file — for every agent except Kilo
  CLI, whose config validator rejects unknown top-level keys outright;
* **one line** on the launcher's stderr when it starts, naming how many models
  and which fields (`limit.context ×4, cost ×8`). Set `MCC_CATALOGUE_VERBOSE=1`
  for the full per-model list, which used to print on every launch;
* the agent's card in **Coding agents**, which is where the Kilo record lives.

What "that CLI's own default" means, per agent:

| Agent | Required-and-unknown becomes | Optional-and-unknown |
| --- | --- | --- |
| OpenCode / OpenCode 2 / Kilo | `limit.context` or `limit.output` → `0`, OpenCode's own unknown marker. The object is all-or-nothing: a `limit` carrying only one of the two makes OpenCode reject the **whole file**, so the known half is kept and the unknown half filled. A model with neither known gets no `limit` at all. | omitted (`cost`, `reasoning`, `tool_call`, `attachment`, `modalities`, `options`, `variants`) |
| Codex CLI | nothing — `context_window` is **optional** in 0.151.0, so an unknown window is omitted, not written as `200000` | omitted (`context_window`, `max_context_window`, `input_modalities`) |
| Pi | `contextWindow` → `128000`, `maxTokens` → `16384`, all four `cost` rates → `0` | omitted |
| Crush | all ten required fields, including `context_window` → `0` and the four `cost_per_1m_*` → `0.0` | omitted (`reasoning_levels`, `default_reasoning_effort`) |
| Kimi Code | `max_context_size` → `0`, Kimi's own marker | omitted (`capabilities`) |
| Qwen Code | nothing required | omitted — but note Qwen then uses its own `DEFAULT_TOKEN_LIMIT = 131072`, not "no gauge" |
| Command Code, Aider, Cline, Droid, Gemini CLI | nothing required beyond identity | omitted |

`0` is a real answer in three of those CLIs and MCC uses it only where the CLI
itself documents it as "unknown" — OpenCode's `limit` coercion, Kimi's
`compute_max_completion_tokens` branch on `<= 0`, Crush's Go zero value. It is
never written as a context window MCC believes in.

**`:batch` refs are excluded from every agent catalogue.** A `:batch` route is
an asynchronous pricing tier of a model you already have, not a second
interactive model, so it doubles a picker for nothing. They remain in
`GET /v1/models`, which answers "what ids may I send?" rather than "what should
I pick". On a real install that is 142 entries down to 82.

**The catalogue obeys `MODEL_VISIBILITY_ALLOW` / `MODEL_VISIBILITY_DENY`**
exactly as `/v1/models` does — same two glob lists, same filter — so a model
hidden on the **Models** page is absent from every agent's catalogue too.

**Three agents have to pin one model to open a session at all** (Cline, Crush
and Goose). They start on the route named by `MODEL`; if that is not among the
visible entries, on the first entry that is not a free tier. Taking the first
entry outright is what they used to do, and on a real install that selected a
free tier the provider had withdrawn, so every session opened on a model that
answered 404.

**Aider is launched with `--no-gitignore`.** Without it Aider appends `.aider*`
to the working tree's `.gitignore`, and `git init`s a directory that is not a
repository — silent edits to your tree that nothing asked for. Pass
`--gitignore` after the command if you want the old behaviour; Aider keeps the
last occurrence.

**Gemini CLI needs `--skip-trust` for a headless run.** MCC does not set it:
workspace trust is the user's decision, not the proxy's. `mcc-gemini -p "..."`
in an untrusted directory exits 55; `mcc-gemini --skip-trust -p "..."` runs.

Kimi Code is the clearest illustration of what "that CLI's own default" means.
`max_context_size` is a *required* integer there, so a model whose context
window nobody published cannot simply omit it — and MCC still refuses to invent
one. It writes `0`, which is Kimi's own marker: `compute_max_completion_tokens`
branches on `max_context_size <= 0` and falls back to its own 32,000-token
budget. The `0` is recorded under `_mcc_defaulted` like every other
substitution.

**Editor integrations** work the same way — Claude Code and Codex in VS Code, or Claude Code through JetBrains ACP. Point them at the proxy address and they behave normally.

---

## 7. Providers and API keys

Open the **Providers** tab. Every provider is one card in a single searchable grid — there are 56 of them, so start by typing in **Search providers**. It matches the provider's name, its id and its environment variable, so `groq`, `GROQ_API_KEY` and `alibaba` all find what you would expect. **Only configured** hides everything you have not set up yet.

<div align="center">
  <img src="../assets/admin-requests.png" alt="Provider configuration in the Admin UI" width="860">
</div>

### The workflow

1. **Find the provider** — search by name or by variable name.
2. **Press Configure.** The card expands and opens that provider's key pool.
3. **Paste your key into "Add key"** and press it. Keys are saved immediately — you do not need **Apply** for them. To add several at once, paste them separated by commas; keys you already have are skipped rather than rejecting the whole paste.
4. **Press Refresh models.** This makes a real API call to that provider. A model count means the key works *and* MCC can read that provider's catalog.
5. **Choose the model** on the **Model Config** tab. There is no "active provider" to select — the model ref you set there decides which provider serves a request.

A provider holds a **pool** of keys, not a single value. Each key in the pool shows its own health (healthy, cooling down, locked out) and has its own **Remove**, which also takes effect immediately. If you added more than one key, pick a **Rotation** policy and press **Apply** — rotation is a restart-required setting, so the server restarts when you apply it.

Local backends (LM Studio, llama.cpp, Ollama) take a base URL instead of a key, and offer **Test connection** where remote providers offer Refresh models.

<a id="using-claude-models"></a>

### Using Claude models

There are two Anthropic providers, and the difference between them matters.

**`anthropic` — a Claude Console API key.** This is the ordinary, supported path: Anthropic's own Messages API, billed per token like any other provider here.

1. Create a key at [platform.claude.com/settings/keys](https://platform.claude.com/settings/keys).
2. Paste it into the **Anthropic (Claude API)** card, exactly like any other provider.
3. Set a model ref such as `anthropic/claude-sonnet-4-6` on **Model Config**.

One thing to know: the override for the upstream address is `ANTHROPIC_UPSTREAM_BASE_URL`, **not** `ANTHROPIC_BASE_URL`. The latter is the variable that points Claude Code *at* MCC — if MCC read it as its own upstream, the proxy would call itself. You almost certainly never need to set either.

**`anthropic_oauth` — a Claude Pro/Max subscription. Anthropic does not permit this.**

Their published terms state that plan OAuth credentials are for Claude Code and Claude.ai only, and that third-party products may not route requests through them. There is no "inside Claude Code" exemption, because once MCC is interposed it is MCC that presents your credential upstream. Anthropic may enforce without notice, and **the risk is to your Claude account**.

If you enable it anyway, MCC refuses by default to use the subscription for anything that did not come from **Claude Code or the Claude Agent SDK** — it reads the `cc_entrypoint` marker Claude Code puts in the request body, so another harness pointed at your proxy is refused rather than silently billed to your plan. Since 6.36.0 the admitted set is `cli`, `cli-bg`, `sdk-cli`, `sdk-py` and `sdk-ts`; before that it was `cli` alone, which refused Anthropic's own SDK.

The Claude subscription card on the **Providers** page reports the plan and rate-limit tier, when the access and refresh tokens expire, the scopes (flagged if `user:inference` is missing, without which the credential cannot answer at all), and the 5-hour and weekly usage windows. Those windows are read from Anthropic's own `anthropic-ratelimit-unified-*` response headers and are never computed: until a real response has carried one, the card says *not yet observed* rather than guessing.

**Read [ANTHROPIC-SUBSCRIPTION.md](ANTHROPIC-SUBSCRIPTION.md) first.** It is the full disclaimer, the settings, and the credential handling.

Claude models also reach you through `bedrock`, `vertex`, and gateways such as `kilo`, `nous_portal` and `cline` — all pay-per-token, none with a policy question attached.

### Reading a failed Refresh models

| Result | Almost always means |
| --- | --- |
| **401 / 403** | The key is wrong, expired, or revoked. |
| **404** | The key is fine — the **model id** isn't available on your account. |
| **402** | Billing: no credit, or plan quota exhausted. |
| **Timeout** | Network, or a self-hosted endpoint that isn't running. |

That 404 case trips people up constantly. If Refresh models fails with 404, check the exact model id against the provider's own model list before assuming the key is bad.

### Doing it by file instead

Set the matching variable in `~/.mcc/.env`:

```bash
NVIDIA_NIM_API_KEY="nvapi-..."
OPEN_ROUTER_API_KEY="sk-or-..."
```

Restart `mcc-server` afterwards.

### Local providers

LM Studio, llama.cpp and Ollama need a base URL rather than a key:

```bash
LM_STUDIO_BASE_URL="http://127.0.0.1:1234/v1"
OLLAMA_BASE_URL="http://127.0.0.1:11434"
```

These take no credentials — the key field stays empty and validation just checks reachability.

### Custom providers

Any OpenAI-compatible endpoint that is not one of the 56 built-in cards can be added by hand. Press **Add custom provider** on the **Providers** tab and give it a display name, a base URL and one API key.

**The base URL must include `/v1` (or whatever path segment your gateway uses).** MCC calls the URL you typed, verbatim — it does not append `/v1` for you. `https://api.example.com/v1` is right; `https://api.example.com` is refused by the gateway on every request — measured against one real host, that is a **403** naming the paths it does serve, not a silent 404, and it surfaces immediately as a red card because the create route probes `/models` before it answers you. Check the gateway's own `curl` example: whatever comes before `/chat/completions` is your base URL.

Creating the provider registers it, hot-reloads the provider runtime and queries `GET <base_url>/models` **once**. What that query returns is what the card reports, what `/v1/models` serves, what the **Models** page counts and what the **Model Config** pickers offer — one discovery, one answer, no restart. If it fails, MCC retries it once and then says so: the card turns red with the upstream's error, and the banner tells you to press Refresh models. A failed discovery never renders as a healthy card.

**Refresh models** on a custom card does exactly what it does on a built-in remote card: re-queries the upstream's model list and republishes every generated harness catalogue, including `~/.mcc/codex-model-catalog.json`. Use it after the upstream adds a model, or after a discovery failure you have since fixed. Enabling a provider, adding a key and removing a key each re-run discovery on their own.

Keys for a custom provider live in **`~/.mcc/custom_providers.json`, not `~/.mcc/.env`.** There is no environment variable for them, so the `{ENV}_API_KEY` / `{ENV}_ROTATION` file workflow does not apply — but the pool itself is the same one built-in providers use, so several keys plus a rotation policy work exactly as they do elsewhere.

**Per-key health.** A custom pool has always rotated, benched and cooled down on the same engine a built-in pool uses; since 6.25.0 the card also *shows* it. Each key row carries the same badge the built-in pools carry — `HEALTHY`, a cooldown with the time remaining, a lockout, or `HEALTHY (1 model)` when the key is rate-limited for one model and still serving every other. Custom pools also appear in the **Rotation, per pool** readout on the Providers tab.

**Disabling and deleting.** These now mean different things, because you mean different things by them.

- **Disable** is temporary, so your routing is left alone and switched off instead: every `MODEL*` chain entry naming the provider is **paused**, exactly as if you had paused it on **Model Config**, and the banner names the entries it paused. Re-enabling lifts precisely those pauses — a ref you paused by hand before disabling stays paused.
- **Delete** is permanent, so the references go with it. Every `MODEL*` key that named the provider is rewritten, and the banner lists what was removed. A route whose own model was the deleted provider is reset to its default rather than left pointing at nothing.

Before 6.25.0 either gesture, applied to a provider a `MODEL*` setting named, made the next settings load fail outright — the whole process, rather than the one route. A disabled custom provider now stays valid in configuration while remaining unbuildable at runtime, which is what "disabled" should have meant all along.

**Reasoning dialect.** This is the one capability a custom provider genuinely could not have. A built-in provider's `reasoning_effort` vocabulary is written into its profile by someone who probed the host; a provider you invented has no profile, so it was assumed to speak the four standard OpenAI words (`minimal`, `low`, `medium`, `high`) — and a host that documents `low`, `high`, `max` had every request for `max` quietly clamped to `high`, with no user-accessible way around it.

MCC now asks the host directly. On create, and again on every **Refresh models**, it sends at most two 16-token chat completions: the first carries a deliberately invalid `reasoning_effort`, and if the host answers `400` naming its enum, that enum becomes this provider's vocabulary. Three outcomes, all shown on the card:

| Card reads | What was established |
| --- | --- |
| `learned {low, high, max} on 2026-09-01` | the host named its enum in a 400; MCC now spells those words |
| `ignored on 2026-09-01` | the host answered 200 to a value no scale contains, so it does not read the field |
| `unknown (401)` / `unknown (not probed)` | nothing was measured; behaviour is unchanged from before |

**Probe reasoning dialect** on the card re-runs it on demand, and the field beside it takes a comma list (`low, high, max`) if you would rather state the answer than measure it; clearing the field forgets what was learned. Nothing about reasoning gating changes: the vocabulary is still intersected with the model's own, a clamp is still recorded in the request log, and an unknown dialect still behaves exactly as it did before.

One caveat on capabilities. models.dev, which supplies context windows, output caps and reasoning-effort vocabularies, is keyed by *its* provider ids — and a provider you invented is not in it. That turns out to be an advantage rather than a gap: without a bucket, custom models fall through to the cross-provider vote (the same model id as served by other providers), which in practice resolves *more* than a bucket does. `supported_parameters` and `default_parameters` are the fields that genuinely stay unknown, and they are unknown for nine of the eleven live providers, built-in ones included. Routing, rotation, key health, benching, fallback chains, visibility globs and analytics are all identical to a built-in provider.

---

## 8. Model tiers and routing

MCC routes by **tier**, not by a single model. Fable, Opus, Sonnet, Haiku and a fallback each map to a real model on your provider.

<div align="center">
  <img src="../assets/admin-model-config.png" alt="Model tier configuration" width="860">
</div>

So when Claude Code requests "Sonnet", it receives whatever you mapped Sonnet to. This is the mechanism that lets an unmodified agent run on any backend.

### Practical advice

**Map Haiku to something cheap and fast.** Agents use the small tier constantly for internal bookkeeping — summarising, classifying, deciding what to do next. A slow model there makes the entire session feel sluggish even when your main model is quick. This single choice affects perceived speed more than anything else in this document.

**Reserve the big tier for actual work.** Opus/Fable should be your strongest available model; you'll hit it far less often than you expect.

**Set the fallback deliberately.** It catches requests for models you haven't mapped. Pointing it at something cheap avoids nasty surprises.

### Fallback chains

Every tier can carry an ordered list of stand-ins. Press **Add fallback** under a tier's model, name a second model, and add a third if you want. When the model a request routes to cannot serve it, the next entry takes over — a free model that rate-limits at an awkward moment stops being the end of the request.

Each chain belongs to its own tier and they are never merged: a tier with its own model tries its own chain, and a tier left on **None** tries `MODEL` and `MODEL_FALLBACKS`.

**Reordering a chain.** Each entry has a grip on its left. Drag it and the row moves; the up/down arrows beside it do the same thing one step at a time and still work, so the whole feature has a keyboard equivalent.

| Gesture | What it does |
| --- | --- |
| Drag a grip within one card | Reorders that chain |
| Ctrl/Cmd-click a row | Adds it to the selection |
| Shift-click a row | Selects the range from the last one you clicked |
| Shift+Space, Shift+Up/Down on a focused grip | The same range, from the keyboard |
| Drag onto another tier's card | **Copies** the model there; the source keeps it |
| Hold **Shift** while dropping on another card | **Moves** it instead — the source loses it |
| Drop onto a card's top slot | That model becomes the route's own model; the one it replaces becomes fallback 1 |
| Escape | Clears the selection, or abandons a drag in progress |
| Ctrl+Z | Undoes the last drag — one level, and only on this page |

A group keeps the order it has on screen, not the order you clicked. Nothing is written until you press **Apply**; the panel at the top of the page says what just happened in a sentence and offers an Undo.

A route's own model is a drag source like any row, but it is never *moved* out of its own card: a route with no model of its own fails validation and the server refuses to start, so that drop is refused with a sentence saying so. Dragging it onto its own first fallback trades the two, which is exactly what its down arrow does.

**Pausing one entry.** Every row, the route's own model included, has a **Pause** button. A paused model keeps its place and stays fully visible with its whole ref, but the router never tries it: **no attempt is spent on it and no deadline is consumed**, and the request log still lists it under *not tried* with the reason `paused`, so a paused route is still debuggable. **Pause is per route.** Pausing an entry on one route does not pause the same model on other routes: a ref that appears in the Opus chain and the Haiku chain is two entries, and pausing the first leaves the second live. Pause it on every route you want it off. The panel names the route it just wrote, which is the route it applies to.

Unlike everything else on this page, a pause is written the moment you click it: there is no Apply, and an unsaved drag elsewhere on the page is left exactly as it was. The status panel offers an Undo. **Since 6.35.1 the click is immediate** — a pause used to rebuild every provider client and re-query every provider's `/models` before it answered, which cost seconds and could briefly stall the proxy; a routing-only write now skips both, because neither can change what a pause does. If a pause fails, the same panel says so and the row goes back to the state it was really in. Pausing every model on a route is allowed and makes that route fail with an error naming the setting, rather than quietly re-routing somewhere you did not ask for.

**Pausing is not hiding.** Hiding a model on the **Models** page only removes it from `/v1/models` and the admin pickers and never changes routing. Pausing only changes routing and never changes listings. They are separate switches on purpose.

| Setting | Holds |
| --- | --- |
| `MODEL_PAUSED` | paused entries on the default route |
| `MODEL_FABLE_PAUSED` | paused entries on Fable |
| `MODEL_OPUS_PAUSED` | paused entries on Opus |
| `MODEL_SONNET_PAUSED` | paused entries on Sonnet |
| `MODEL_HAIKU_PAUSED` | paused entries on Haiku |
| `MODEL_VISION_PAUSED` | paused entries on the vision adapter |

All six are comma-separated `provider/model` lists, written by the Pause button rather than typed, and **new in 6.21.0**. An entry is dropped from its list automatically when it leaves the route it was paused on.

**Failover stops once you have seen output.** This is the part people get wrong:

| The model fails… | What happens |
| --- | --- |
| while connecting, authenticating, or rate-limiting | the next model takes over, invisibly |
| before it emits anything | the next model takes over, invisibly |
| halfway through streaming its answer | the request fails |
| at any point, for a **non-streaming** request | the next model takes over — nothing reached you yet |

A chain rescues the failures that happen before the first word, not the ones that happen at word five hundred. Switching models mid-answer would splice two different replies together, so MCC refuses to.

**A model that goes quiet is a failure too.** Accepting a request and then producing nothing looks, to a proxy with no deadline, exactly like thinking hard — so without a limit it holds the request until the transport gives up, and the chain gets its turn minutes later. Four settings on the **Limits & Resilience** tab bound that:

| Setting | Default | What it does |
| --- | --- | --- |
| First-token deadline | `120s` | How long a model may stay silent before the next one takes over. Nothing has streamed yet, so you never see the switch. |
| Total request budget | `600s` | The whole request, across every attempt and retry. A stream that already started cannot be replaced, but it can be stopped. |
| Eject mode | `rate_based` | How a failing **model** is benched. `rate_based` (default) skips a model when its failure rate over the last `FALLBACK_EJECT_WINDOW` requests (default 10) crosses `FALLBACK_EJECT_FAILURE_RATE` (default 50%), with at least `FALLBACK_EJECT_MIN_SAMPLES` (default 8) requests observed, for `FALLBACK_EJECT_SECONDS` (default **30 s** — until 6.0.0 a clamp inside route health silently cut that to 1 s for timeout and 5xx ejections, so a model you thought was benched for half a minute was back at the front of the chain a second later). A single blip never benches a working model; sustained failures do. `legacy` preserves the old consecutive-count behaviour keyed on `FALLBACK_EJECT_AFTER_FAILURES` / `FALLBACK_EJECT_SECONDS`. This is about models. It never touches a key — see [Multi-key rotation](#11-multi-key-rotation). |
| Retry primary once | `skip` | What happens when the primary model fails. `skip` (default) moves straight to the next fallback. `retry_once` gives the primary one more chance for transient errors (timeout, 5xx, 429) before falling through. Auth and invalid-request errors are never retried. |

If every model on a route is benched, MCC tries them in order anyway — skipping a bad model is an optimisation, refusing to try anything is an outage.

**Running out of context no longer ends the chain.** A conversation that outgrew a model's window and a genuinely malformed request both come back as HTTP `400`, and until 5.43.0 MCC treated them alike: it gave up on the whole chain, on the reasoning that a bad request will be bad everywhere. That is true of a malformed body and false of a context overflow, which is precisely what a larger-window fallback is for. MCC now tells the two apart and falls through to the next model on an overflow. If you preferred the old behaviour, set `FALLBACK_SKIP_KINDS=invalid_request,context_length` to abort on both again.

**Nor does running out of credits.** Since 6.34.0 an account with no balance left is its own failure kind, `quota`. It is what the provider *says*, not what status it picks: HTTP 402, or a 400/403 whose error body names an explicit billing phrase — `insufficient credits`, `purchase more credits`, `insufficient balance`, `NOT_ENOUGH_BALANCE`, `quota exceeded`, `insufficient_quota`, `payment required`, `out of credits`, `credit balance is too low`, `CreditsError`. One measured request on 2026-09-02 asked for `commandcode/z-ai/glm-5.3-flash`, got HTTP 400 *"You have insufficient credits to make this request. Please purchase more credits to continue using the service."*, and — because that body classified as `invalid_request`, which is in `FALLBACK_SKIP_KINDS` by default — six configured chain entries were recorded as *not tried* and the caller got a 400 for a request that was perfectly fine.

**What "credits exhausted" now does.** Another key may have credits, so the pool rotates first; when every key is out, the chain moves to the next model. It never counts toward the chain bench — an empty wallet says nothing about the model. When the phrase is one of the list above, the key is also **benched for `RATE_LIMIT_COOLDOWN_SECONDS`** (the same setting a header-less 429 uses; no new number), whole-key rather than per-model, and the Models page says so: *COOLDOWN — credits exhausted*, with *benched: credits exhausted, 55s left* on hover. A bare 402 with no recognisable phrase still rotates and still falls through, but benches nothing: unsure never benches. If every model on the route ends this way, the error you get names it — *all keys reported exhausted credits* — and points at **Providers**.

Requests that name a provider and model directly (`open_router/…`) are never redirected. An explicit choice is honoured as given.

<a id="tiers-for-every-other-coding-agent"></a>

### Tiers for every other coding agent

Everything above this line has been Claude Code's privilege. Claude Code never
names a model: it asks for `claude-sonnet-5` and receives whatever you mapped
Sonnet to, which is why moving a route moves every session that is already
running on it. Every other agent had to name a concrete `provider/model` ref,
because that was the only thing that existed for it — and the request log is
blunt about the consequence. Across the whole 272,132-row request log the number
of non-Claude-Code requests that ever named a tier-style alias is **zero** —
every coding-agent request named a concrete provider/model ref, because that was
the only thing that existed for them, while 99.98% of Claude Code traffic names
an alias. A model id typed into OpenCode's config a month ago is still that
model id today, however many times you have moved the route it should have been
following.

**New in 6.38.0**, five names close that gap. They sit at the top of the model
picker MCC generates for each of the thirteen agents that carry a catalogue —
Codex, Pi, OpenCode, OpenCode 2, Kilo, Command Code, Kimi Code, Qwen Code,
Crush, Cline, Aider, Droid and Gemini CLI — and each one is a name for a route
on this page rather than a model of its own:

| Name | The route it names | And therefore the chain it uses |
| --- | --- | --- |
| `mcc/best` | `MODEL` | `MODEL_FALLBACKS`, `MODEL_PAUSED` |
| `mcc/good` | `MODEL_OPUS` | `MODEL_OPUS_FALLBACKS`, `MODEL_OPUS_PAUSED` |
| `mcc/medium` | `MODEL_SONNET` | `MODEL_SONNET_FALLBACKS`, `MODEL_SONNET_PAUSED` |
| `mcc/cheap` | `MODEL_HAIKU` | `MODEL_HAIKU_FALLBACKS`, `MODEL_HAIKU_PAUSED` |
| `mcc/vision` | `MODEL_VISION` | `MODEL_VISION_FALLBACKS`, `MODEL_VISION_PAUSED` |

The fleet is genuinely split over how it spells a model id, so both spellings
are accepted: the bare `mcc/best` that Codex, Command Code, OpenCode, Kilo, Pi
and Kimi Code put on the wire, and the gateway form `anthropic/mcc/best` that
Cline, Crush, Droid, Gemini CLI, Qwen Code and Aider put on it. The router
answers the same way to both, and the tier segment is matched exactly — no name
merely *containing* `cheap` lands on the cheap rail.

**On a default install all five resolve to the same model, and the dashboard
says so.** `MODEL_OPUS`, `MODEL_SONNET`, `MODEL_HAIKU` and `MODEL_VISION` ship
unset, so every tier collapses onto `MODEL` — primary, fallbacks and pause list
together — which is exactly what `claude-opus-5` already does today. MCC
deliberately does not invent a different model for an unset tier, because
choosing one would be MCC choosing a model for you; the Tiers section says *Same
as global Opus — currently `<ref>`* instead of quietly picking something. Map
Opus, Sonnet and Haiku the way section 8 recommends and the five names separate
by themselves, with no further configuration.

**Giving one agent its own tier.** Each card on the **Coding agents** page now
carries a collapsible **Tiers** section built from the same rail component as
Model Config: drag a row by its grip to reorder the chain, **Pause** one entry
without deleting it, and drop a fallback onto the top slot to promote it to the
route's own model. **Override** on a tier starts that agent's own chain,
**Revert to global** removes it again. The result is written atomically to
`~/.mcc/harness_tiers.json`, which you can also read or edit by hand:

```json
{
  "harnesses": {
    "opencode": {
      "best":  {"model": "open_router/x-ai/grok-5",
                "fallbacks": ["nous_portal/tencent/hy3"],
                "paused": []},
      "cheap": {"fallbacks": ["open_router/z/cheap-1"]}
    }
  }
}
```

There are three states per agent and tier, and the middle one is the point of
the file:

| What the file says | What the agent gets |
| --- | --- |
| no entry for that agent, or none for that tier | the global chain — the default for every agent and every tier |
| an entry with no `model` | the global primary still leads, but this agent's own `fallbacks` and `paused` follow it |
| an entry with `model` set | this agent's own chain leads |

An entry that names its own `model` owns its **whole** chain: the global
fallbacks are not appended underneath it, because attaching models you never
listed under a heading that says these are yours would be the more surprising
answer. A ref in the `mcc/` namespace is refused — a tier can never point at
another tier — and an entry for an agent id MCC does not know is dropped with a
log line rather than honoured against nothing.

**A request with no agent identity still resolves.** Per-agent overrides need to
know which agent is asking, which MCC learns from the `x-mcc-harness` header its
launchers send or from the user-agent fingerprinting added in 6.37.0. When
neither answers — raw `curl`, an older launcher — the tier still resolves; it
simply resolves against the global chain. The feature never fails a request for
want of an identity.

**Turning the names off.** `HARNESS_TIER_ALIASES` (default `true`) is the master
switch for whether the five aliases are emitted into the generated pickers and
into `/v1/models`. It is on **Model Config**, under the models section, as *List
MCC tiers in coding agents*. Switching it off keeps the pickers to concrete refs
only; it does **not** stop the router resolving a tier alias a client sends
anyway, so an agent whose config already names one keeps working either way. For
the same reason `MODEL_VISIBILITY_ALLOW` and `MODEL_VISIBILITY_DENY` never
filter these five: they are protocol names for MCC's own routes, exactly like
the eight `claude-*` aliases, so filtering one would not hide a model — it would
break an agent whose config file already names it.

**What else moved with them.** Crush now seeds `models.large` on `mcc/best` and
`models.small` on `mcc/cheap`; it used to repeat the same model for both,
because inventing a "small" model by matching on a name would have been MCC
guessing which of your routes is cheap, and `mcc/cheap` is not a guess — it is
the route you labelled cheap. Cline seeds its session model on `mcc/best` for
the same reason. Gemini CLI's seeded model was simply a bug: it took the first
entry of the enumeration rather than the route you configured, so a session
opened on whichever model happened to enumerate first; it now goes through the
same starting-model rule as Cline and Crush. Kimi Code prefixes every ref with
`mcc/`, so a tier's generated key there reads `mcc/mcc/best` while the `model`
value on the wire is the plain `mcc/best`. Claude Code is unaffected — it has no
generated catalogue and keeps speaking the `claude-*` names. And the provider id
`mcc` is now reserved: creating a custom provider whose name would slug to it is
refused with a message saying why, rather than shadowing all five aliases and
leaving that provider's models silently unroutable.

**Finding one in the log.** A request that named a tier is recorded with
`requested_model=mcc/best`, `harness=<agent id>` and `resolved_model=<the real
ref>`, plus `tier`, `tier_source` (`global` or `override`) and `tier_harness` in
the row's parameters — enough to answer the question `resolved_model` alone
cannot, which is whether *your* override fired or the agent quietly followed the
global route. Nothing about the log's schema changed and exports are unchanged.

<a id="how-an-agents-model-list-gets-to-disk"></a>

### How an agent's model list gets to disk

Every agent's generated document lives under `~/.mcc` — `~/.mcc/opencode-config.json`,
`~/.mcc/crush/crush.json`, `~/.mcc/kimi-code-config.toml` and the rest, one per
agent, all listed on the **Coding agents** page with the path and the time it was
last written. They are MCC's files in MCC's own directory; nothing is ever
written inside a CLI's own configuration folder.

**`mcc-server` writes all of them at startup, and rewrites them on every
catalogue publish.** A launcher's job is therefore to open the file and start
the agent. It costs no HTTP at all, so a launch is as fast as the agent itself.

**A launcher fetches only to create a document that is not there** — a machine
where `mcc-server` has never run, or a file you deleted. That single build gets
`CATALOGUE_FETCH_TIMEOUT_SECONDS` (20 s by default, on **Limits & Resilience →
Deadlines**), and if it fails the warning names the file it wanted, the request
it tried, the budget it had and what to do next; the agent then starts without
MCC's provider rather than refusing to start.

Two agents work differently, on purpose:

* **Command Code** has no extra-config variable, so MCC owns one key inside
  `~/.commandcode/providers.json` — a file *you* wrote. That merge happens on
  every `mcc-commandcode` launch and never in the background: a `provider.mcc`
  block must not appear in your own config because a key rotated on a server you
  left running.
* **Goose** has no generated file anywhere. It takes its model and context limit
  from environment variables the launcher sets, so it reads the model list from
  the server on each launch.

> **Before 6.36.1 this was the other way round**, and it deadlocked. Only the
> launcher created a file, and its fetch was given the 1.5 s budget belonging to
> the `GET /health` preflight — for a route that takes 1.8–4.0 s on a real
> install. So the fetch timed out, the file was never created, the server never
> refreshed a file that did not exist, and every later launch failed the same
> way: *"could not prepare the OpenCode config (timed out); launching without an
> MCC provider"*, forever.

### The Codex App catalog

The Codex App has no launcher — it reads a persistent `~/.codex/config.toml` rather than an environment built per command. Its catalogue is written the same way every other agent's is: `mcc-server` writes `~/.mcc/codex-model-catalog.json` at startup and rewrites it whenever the model inventory *or any model's resolved capabilities* change. The Codex App points at that stable path from its config:

```toml
model_catalog_json = "/Users/YOUR_USERNAME/.fcc/codex-model-catalog.json"   # macOS
# model_catalog_json = "C:/Users/YOUR_USERNAME/.fcc/codex-model-catalog.json"  # Windows

model_provider = "fcc"
model = "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"

[model_providers.fcc]
name = "My Claude Code"
base_url = "http://127.0.0.1:8082/v1"
env_key = "FCC_CODEX_API_KEY"
wire_api = "responses"
```

`env_key` reads the same proxy auth token the `mcc-codex` launcher sets per process. Because the server owns this copy of the catalog, the Codex App always sees the current model list — restart it after setup or a model change, then pick an MCC model from its picker.

### Images and the vision adapter

Plenty of fast text-only models cannot read a screenshot. Set a **Vision adapter** on Model Config and any request carrying an image goes there instead — but only when the tier's own model is *known* not to accept images. A model whose provider publishes no capability data is left alone, because rerouting on silence would move traffic away from models that handle images perfectly well.

You do not have to work out which of your models are affected: a tier that needs the adapter says so on its own card, naming where its images actually go. If no adapter is set, the same line turns amber to say those images will fail there.

The adapter is a route like any other, so it gets its own **Add fallback** chain. One unreachable vision model would otherwise lose every image on the machine.

<a id="tutorial-manage-many-models"></a>

### Tutorial: manage many models

A provider with three hundred models is not manageable one tick at a time. On a real catalogue of 1,021 models across 10 providers, hiding the 317 published by one gateway used to be 325 clicks and 634 requests — about 1.07 GB of traffic and five to nine minutes of clicking. The same job is now two interactions and one request: 41 KB, 7 ms.

<div align="center">
  <img src="../assets/admin-models-bulk.png" alt="Models page with rows selected and the bulk action bar open" width="860">
</div>

**1. Open Admin UI → Models.** Each provider is a collapsed disclosure with a sticky header carrying its visible / hidden / configured counts. The header buttons — **Show all**, **Hide all**, **Invert** — work while the provider is still collapsed; you never have to expand 317 rows to act on them.

**2. Press `/` to search.** It focuses the filter from anywhere on the page. Type `opus`, or a provider id, or a fragment of a model name. The facet chips — All / Visible / Hidden / Configured / Overridden — narrow the same list, and the result-count line offers **Select all N**.

**3. Apply to what the filter left.** This is the point of the filter: the bulk buttons act on the *filtered* set, not the whole catalogue. "Hide the 38 models matching `opus` across four providers" is three interactions — filter, Select all 38, Hide.

**4. Or pick a range by hand.** Every row has one checkbox, in the ruled left gutter. Beside it is a readout — `Shown`, `Hidden`, or `Hidden by <pattern>` — which reports the row's state and is not a second control:

| Gesture | Selects |
| --- | --- |
| Click, then **Shift+click** | everything between the two rows |
| **Shift+ArrowDown / Shift+ArrowUp** | extends or shrinks the same range from the keyboard |
| **Shift+Space** | extends the selection from the anchor row to the focused row — the keyboard equivalent of Shift+click; a single row only when no anchor exists |
| Press and **drag down the gutter** | every row the pointer crosses |

A drag never leaves a mixed run — swept rows all take the anchor row's state. The provider header checkbox is tri-state, and the action bar counts what you have picked as you pick it. The keyboard gestures are not decoration: a pointer drag alone would fail WCAG 2.2's dragging-movements rule.

**5. `Escape` clears the selection.** Only when no dialog is open, though — inside the request or export modal, Escape still belongs to the modal. With nothing selected, Escape simply drops focus out of the filter.

**6. Confirm, if it is a big one.** At **200 models or more** the button asks once, in place: it relabels itself `Hide all 317 — confirm` and waits. Press it again to go ahead, or leave it and it reverts after five seconds. There is no modal — visibility is display-only and reversible, and a dialog on every Hide all is exactly the friction being removed.

**7. Read the status panel.** One `role="status"` panel, not a toast: it stays until you dismiss it, because a message that names a pattern you may want to copy has no business vanishing on a three-second timer. It reads, for example: *Hid 317 of 317 nous_portal model(s). Written as one pattern, `nous_portal/*`. Routing is unaffected either way.*

#### The part that is not obvious: what gets written

The bulk actions edit the same `MODEL_VISIBILITY_DENY` / `MODEL_VISIBILITY_ALLOW` lists you can type into by hand, and *which shape* they write is a deliberate distinction:

- **Hide all on a whole provider writes ONE glob**, `nous_portal/*`, as a standing policy. Models that provider publishes next week are hidden on arrival. **Show all** removes that glob again. Running Hide all on an already-globbed provider is idempotent and reports that nothing was written.
- **A selection, or a provider narrowed by a filter, writes exact refs** — one per model. A hand-picked set is a fact, not a policy, and no glob describes it without also hiding something you did not choose. **Invert** writes exact refs for the same reason.
- **A glob you wrote yourself is never deleted on your behalf.** If `*:free` or `nous*` still hides a model after a Show all, the panel says so once per offending pattern — *12 of them did not change: your pattern `*:free` overrules an exact tick* — with a **Show the 12** button that filters the list down to exactly those rows. One pattern named once, not 317 rows reported individually. Those rows also say it themselves, permanently: their readout reads `Hidden by *:free`, and clicking the pattern offers to remove it.
- **Hide all shadows your per-model choices; it does not delete them.** Writing `nous_portal/*` leaves the exact patterns underneath it in place, and the first **Show all** afterwards lifts only the glob — so the per-model state you had before the Hide all comes back exactly. Press **Show all** again and it clears those exact patterns too. Hide all → Show all is an identity; Show all → Show all is "show everything".

**Undo** sits in the same panel and restores both pattern lists exactly as they were, in one click. It restores the *lists*; it cannot undo a hand edit you made to the pattern fields since. And none of this changes routing — hiding is display-only, so a hidden model you have configured still resolves, still serves, and still appears in your agent's picker.

<a id="hiding-models"></a>

#### Hiding models: the three rules

Everything above reduces to three sentences, and they are the whole model:

1. **The allow list is opt-in when it is not empty.** `MODEL_VISIBILITY_ALLOW` empty means "list everything". The moment it names one pattern, every model it does *not* name is hidden.
2. **The deny list is applied after the allow list, and it wins.** `MODEL_VISIBILITY_DENY` cannot be overruled by anything in the allow list, which is why an exact tick can fail against a glob: showing `nous_portal/aion-2.0` writes its exact ref, and `nous_portal/*` still hides it. The row says so — `Hidden by nous_portal/*` — instead of springing back with no explanation.
3. **Hiding never affects routing.** A hidden model named in `MODEL`, in a tier override, or in a `MODEL_*_FALLBACKS` chain still resolves and still serves. Hiding removes it from `/v1/models` and from the pickers; that is all it does. A visibility filter that silently broke a working chain would be worse than a chain entry that is invisible but alive, because the breakage would surface as an outage nowhere near the setting that caused it.

Both lists are comma-separated globs matched case-insensitively against the full `provider/model` ref, with `*`, `?` and `[...]`. `*` crosses `/`, so `nous_portal/*` covers `nous_portal/anthropic/claude-opus-4.6` as well as `nous_portal/aion-2.0`.

#### One mechanism, one write path

There is exactly one way to change what the catalogue shows from this page: select rows in the gutter and press a button in the action bar. That is true for three hundred rows and it is true for one — a single row goes out on the same batched request, so there is one endpoint, one repaint and one owner of what the page is holding. The readout beside each checkbox is a *state* (`Shown` / `Hidden`), never a verb, because a word that reads like a command in the slot that reports what is true makes a row that did not change look like a control that did not respond.

The action bar says how much of the work is already done — *Hide 3 selected (2 already hidden)* — rather than offering a tri-state control. Hide on a mixed selection hides all of it; **Invert** is computed against the state at the moment you click, before anything is written.

#### Folding a thousand exact patterns into globs

Ticking models one at a time writes one exact pattern each, and that adds up: a real install reached **994 exact deny patterns and not a single glob** — a ~30 KB line in the managed env file, parsed and rewritten on every write. **Migrate exact patterns to globs**, beside *Save patterns*, folds every provider whose models are *all* individually hidden into one `provider/*`.

It is offered, never applied on its own. Pressing it previews: how many patterns become how many, which providers, and how many models are hidden before and after. The fold is only offered when those last two numbers are equal — the migration is verified model by model and abandoned whole if it would move even one — and the write that follows is undoable from the same panel. One thing does change going forward: a `provider/*` glob also hides models that provider publishes *later*, which is the point of a policy and is worth knowing before you accept it.

### Reasoning control

Providers expose reasoning differently. MCC resolves your intent once at the boundary and each provider adapter translates it, so you configure it in one place rather than per provider. See the Model Config tab.

Two independent facts decide what actually goes on the wire: what the **model** supports, and which reasoning fields the **host** in front of it parses. A control is sent only when both agree; otherwise the nearest thing both can express goes instead, and the request log names the field it went through. A model with only an on/off switch behind a gateway whose only word for "reason" is one of its own effort rungs gets the level you asked for, clamped to that gateway's scale — the gateway's *own* default rung is never put in its place, so a request for `low` never leaves as `max`. Where the host has no reasoning field at all, nothing is sent and the model's own default applies; the request log calls that "no reasoning instruction sent (model default applies)", and it is a correct outcome, not a fault. Where MCC knows neither fact the request is unchanged.

Every OpenAI-compatible host declares the standard `reasoning_effort` field unless it was probed speaking something else; a host that refuses it answers with a 400, is retried once without it, and is not asked again for that model. Since 6.33.0 that is true of **every** provider, whichever protocol it speaks: an Anthropic Messages host that refuses a `thinking` object and a Responses host that refuses a `reasoning` block are learned from the same way, by the same matcher. Your own model-parameter override is applied **after** every postprocessor, so setting `reasoning_effort` explicitly — or to null — on a model always wins over the default dialect.

The Models page shows the two side by side: what the model can do, with the resolution tier each field came from, and what the host parses, labelled **default OpenAI dialect**, **declared by this provider**, or **learned from the host's own rejection** — never a vote.

Since 6.35.0 the tier is shown for **every** resolved field, not only the output ceiling: the context window, vision support, tool support and all four price rates each walk the same ten rungs and each name the one that answered. Where the routed deployment's own `/models` payload supplied the value the badge reads *provider /models or models.dev* with no tier, because the enrichment step that merged them keeps no record of which won; where the ladder supplied it, the rung is exact — down to *cross-provider, bare model*, which is a vote across strangers who merely share a name and is badged **approximate** accordingly. The Models page and every generated agent catalogue read that from the same resolver, so they cannot disagree about a number or about where it came from.

#### What "learned from the host's own rejection" means

It means exactly one thing: this host answered a real request with a 400 whose own words named that reasoning field, the request was retried without it and **succeeded**, and the field is not sent to that model again. It is never inferred from a model name, never voted on across providers, and never written from a retry that failed anyway — a strip that did not fix the request is no evidence the field was the problem. The date beside the label is the day it was learned.

The same holds for the output cap a host states in a 400: the number is read out of the host's own message, applied to that request, and used to clamp later ones. It only ever lowers what is asked for.

**Both memories are per process, and that is deliberate.** They live on the provider instance, so a config reload, a restart, or an update rebuilds the provider and forgets everything it had learned. Nothing is written to `~/.mcc`. A host that was briefly misconfigured therefore heals by itself rather than staying blacklisted until someone notices — at the cost of paying each 400 once more after a restart, which is one request per model.

A 400 that names a **sampling** parameter — `top_p`, `temperature`, `seed` — is never treated as a reasoning rejection: dropping thinking would not have fixed it, so the error is raised. So is a 400 that names nothing recognisable at all; Command Code's Anthropic endpoint answers a malformed `thinking` value with a bare `Invalid input`, and a gateway that vague gets a visible failure rather than a guess.

---

## 9. Web search

Claude Code's `web_search` is an Anthropic **server tool**: normally Anthropic executes the search and bills you for it. MCC intercepts and fulfils it locally against a provider you choose, so **no Anthropic search credits are used**, and it works with any model provider.

<div align="center">
  <img src="../assets/admin-websearch.png" alt="Web search configuration and analytics" width="860">
</div>

### Choosing a provider

```bash
WEB_SEARCH_PROVIDER=auto            # auto | off | disabled | <provider id>
WEB_SEARCH_FALLBACK_POLICY=auto     # auto | none | ddgs | legacy
```

**`auto` works with zero configuration.** With no keys set it falls back to keyless DuckDuckGo, so search works out of the box. Set any provider key and `auto` prefers it.

A missing API key on an explicitly selected provider **fails visibly** rather than silently degrading — an unconfigured provider is an operator mistake, not an outage.

14 backends are supported. Free tiers worth knowing: Exa ($10/month ongoing), Tavily (1,000 credits/month), Brave ($5/month), Serper (2,500 one-time), Linkup ($20 topped up monthly), and DuckDuckGo (keyless, unlimited, lower quality).

### The setting most worth changing

By default most providers return a one-or-two sentence **snippet**. Several can return the **extracted text of the page** — the difference between the model guessing from a summary and actually reading the source.

Turn it on for your provider, then give it room to reach the model:

```bash
# Pick the one matching your provider:
EXA_CONTENTS=text                    # or highlights+text, full
TAVILY_INCLUDE_RAW_CONTENT=markdown  # or text
FIRECRAWL_SCRAPE_FORMAT=markdown     # or summary
BRAVE_EXTRA_SNIPPETS=true            # plan-gated

# How much of it actually reaches the model:
WEBSEARCH_DIGEST_CONTENT_CHARS=4000
```

Jina, Parallel and Linkup return extracted text by default and need no switch.

Extracted text has its **own cap**, separate from the snippet cap, so opting in isn't silently trimmed back to snippet length. Set it to `0` to keep snippets only.

> **Cost:** content options bill more on most providers — Firecrawl multiplies credits per result, Exa charges per content type — and they increase input tokens on **every** search. Each option's drawer in the Admin UI states its cost.

### Restricting searches to specific sites

Claude Code declares `allowed_domains`, `blocked_domains` and `max_uses` on its `web_search` tool. MCC reads them and forwards them:

```json
{
  "type": "web_search_20250305",
  "name": "web_search",
  "allowed_domains": ["docs.python.org", "peps.python.org"]
}
```

This filters **server-side** on Exa, Tavily, Firecrawl, Linkup, Perplexity and Parallel — you pay for relevant results instead of filtering after the fact. Providers without native support search normally and drop the filters; every recorded attempt shows `supports_domain_filters`, so the analytics detail tells you which happened.

Anthropic rejects requests carrying both lists, so if both arrive the allow list wins rather than being silently intersected.

### Safe search, locale, freshness

```bash
BRAVE_SAFESEARCH=strict       # off | moderate | strict
SEARXNG_SAFESEARCH=2          # 0 | 1 | 2
SERPAPI_SAFE=active
SEARCHAPI_SAFE=active
DDGS_SAFESEARCH=strict
```

Locale matters if you're not in the US — **Firecrawl returns US results unless told otherwise**:

```bash
FIRECRAWL_COUNTRY=DE
TAVILY_COUNTRY=germany
BRAVE_COUNTRY=DE
SERPER_GL=de                  # SERPAPI_GL / SEARCHAPI_GL / JINA_GL are equivalent
```

Freshness uses each provider's own vocabulary (`BRAVE_FRESHNESS=pw`, `TAVILY_TIME_RANGE=week`, `SERPER_TBS=qdr:w`). For a precise window rather than a relative one:

```bash
TAVILY_START_DATE=2026-01-01
TAVILY_END_DATE=2026-06-30
LINKUP_FROM_DATE=2026-01-01
EXA_START_PUBLISHED_DATE=2026-01-01
```

### Two options especially useful for coding

```bash
FIRECRAWL_CATEGORIES=github,research   # restrict to GitHub or papers
TAVILY_CHUNKS_PER_SOURCE=3             # more text per source, cheaply
```

All **66** advanced options are editable from the Web Search tab's **Advanced options** drawers, and every one states what leaving it blank does.

---

## 10. Analytics

Two separate local SQLite stores under `~/.mcc/logs/`, both written by a background thread so they never block a request.

### Model requests

<div align="center">
  <img src="../assets/admin-analytics.png" alt="Model request analytics" width="860">
</div>

Summary cards cover volume, success and error rate, latency percentiles, time-to-first-token and token usage. Below: requests over time, tokens by model, and per-provider and per-key tables. Counts, sums and averages are exact; the p50 and p95 latency cards are interpolated from a 64-bucket log-spaced histogram (measured at or under 2.3% error on a 244k-request log), which is what lets an all-time view load in a fraction of a second. A time range that does not start and end on a whole UTC hour is widened outward to one.

<div align="center">
  <img src="../assets/admin-key-performance.png" alt="Per-key performance breakdown" width="860">
</div>

#### Reading the token columns

Input is reported in two parts, because cached and uncached prompt tokens bill differently:

| Column | Meaning |
| --- | --- |
| **Input (uncached)** | prompt tokens the provider actually processed |
| **Cached input** | prompt tokens served from the provider's cache |
| **Cache hit rate** | cached ÷ total input |
| **Cache writes** | tokens written into the cache |

> **A hit rate of `—` means that provider never reported caching at all** — which is different from a measured `0.0%`.
>
> Prompt caching is provider-dependent. OpenAI reports it for prefixes of 1,024+ tokens; DeepSeek reports it with its own fields. **NVIDIA NIM's hosted endpoint does not do real prefix caching** — it returns a small constant regardless of repetition — so a near-zero rate there is accurate rather than a fault. NVIDIA exposes prefix caching as a self-hosted deployment toggle (`NIM_ENABLE_KV_CACHE_REUSE`), not on the shared API.

#### Finding a request again

**Search text** matches across everything a request contains, not just the visible prompt and reply:

| Searched | |
| --- | --- |
| Prompt | what you sent |
| Reply | what the model answered |
| **Reasoning** | the model's thinking blocks |
| **Tool calls** | tool names and their arguments — commands, paths, patterns |

Reasoning and tool calls are the majority of a real log: on a typical machine 55% of requests carry thinking text and 78% carry tool calls. Before v4.46.0 neither was searched, so a term that appeared only in a command you ran returned nothing.

**Every word must appear, in any order and anywhere in the request.** Searching `proxy 8082` finds a request that says "restart the proxy" in the prompt and "port 8082 is busy" in the reasoning. A single word behaves exactly as before. Matching is case-insensitive and by substring, so `kube` finds `kubernetes`.

#### Which model actually answered

A request does not always go where the tier points. **View** on any row draws the whole path it took:

```
nous_portal/tencent/hy3:free                    failed
nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b    failed
opencode/deepseek-v4-flash-free                 answered
```

The chain is recorded even when your first choice answers, so you can confirm your fallbacks are configured without waiting for something to break. Rows carry a `fallback N` badge when a stand-in served them, and a `vision` badge when the vision adapter took the request instead — those say, in a sentence, which model could not read the image.

Two panels summarise it across the window: **Failover** pairs each failing primary with what covered for it, and **Vision adapter** does the same for image diversions. The **Served by fallback** card shows how often the safety net engaged; a `—` there means no request in the window recorded routing data at all, which is different from a measured zero.

Requests logged before v4.42.0 have no chain recorded, so the panel is hidden for them rather than inventing one.

Every row's dialog also shows the full request and response, the resolved configuration, and timing. It's usually the fastest way to see what actually happened.

#### Exporting

**Export** opens the export window. It covers Model requests and Web Search, in four formats — JSON, CSV, XLSX and TXT — and streams the **entire** matching row set (it is not capped at the 500-row list page). Pick a period (last hour, 24 hours, 7 days, 30 days, or a custom from–to range), choose which fields to include, and optionally group the output by provider, period, model or key — with the grouping order selectable (e.g. Provider → Period → Model, or Period → Provider → Model). Selecting body-bearing fields (Input, Output, Tool calls, Thinking) includes the stored text for each row.

#### Why the totals stop rising

The request detail view shows the outbound body per attempt: sampling and reasoning parameters are stored whole however large the body is, and only the message and tool structure degrades to counts and names under `REQUEST_LOG_WIRE_BODY_MAX_CHARS`. On the Models page, *reasoning requested* and *reasoning returned* are independent measurements — what left, and whether thinking text came back.

`REQUEST_LOG_MAX_ROWS` caps **stored rows**. Once the table is full, one row is deleted for every row that arrives, so everything computed from those rows is a rolling window:

| Section | Covers | Affected by retention |
| --- | --- | --- |
| Summary cards, charts, tables | the filter row and time range | **yes** — frozen once the cap is reached |
| **All time** | every request ever completed | no — never pruned |

At the cap, Analytics says so above the cards rather than letting a frozen counter look like a bug. **All time** is a small permanent rollup kept per day, provider and model, so per-model request counts and token usage keep climbing after stored rows roll over. It ignores the filters and the time range on purpose.

Two things worth knowing about it:

- Upgrading seeds it from whatever history is still retained. Rows pruned before the upgrade are gone and cannot be recovered, so the two figures start out equal and diverge from then on.
- **Clear log** erases it too. It is an explicit "erase my history" action, and reporting millions of all-time requests over an empty table would read as a bug.

#### Sizing the cap

Bodies are **99% of the stored bytes** — about 30 KB of text per row against 332 bytes of metadata — so retention is really a disk decision.

They are therefore stored zstd-compressed in a side table, against a dictionary trained on your own traffic. That dictionary is what does the work: consecutive requests repeat a near-identical system prompt and conversation history, and per-row compression cannot see across rows. Replaying 4,000 real requests through both paths:

| | database | per row |
| --- | --- | --- |
| Inline text | 168.5 MB | 41.1 KB |
| Compressed | **28.2 MB** | **6.9 KB** |

Roughly **6× more retention for the same disk**. A body costs ~24 µs to read back, and search still matches inside compressed text.

```bash
REQUEST_LOG_ENABLED=true
REQUEST_LOG_MAX_ROWS=50000         # oldest rows pruned beyond this
REQUEST_LOG_COMPRESS_BODIES=true   # false stores text inline, as before
REQUEST_LOG_CAPTURE_BODIES=true    # false drops text entirely, ~77x more rows/GB
REQUEST_LOG_TEXT_MAX_CHARS=50000   # longer text is truncated before storage
REQUEST_LOG_WIRE_BODY_MAX_CHARS=8000  # bounds stored message/tool structure only
REQUEST_LOG_COMPRESSION_LEVEL=9    # 1-22; 19 measured 4.9% smaller at 9x the time
```

All of these are editable in **Admin UI → Analytics** without touching the file — they are the **Request log storage** card at the bottom of the same page whose contents they govern. Leaving one blank means "use the default" rather than "invalid", so clearing a field can never stop the server starting, and a value outside its range is refused by the form and clamped (with a warning) if it was edited into the file by hand.

Two things not to worry about: the dictionary trains itself once the log has seen a few hundred requests, and each blob records which dictionary compressed it, so retraining never orphans an older row.

#### Compacting a log that predates compression

Compression only ever applies to **newly written** requests, so a database carried across the upgrade keeps paying the old price for its whole history. On a real 1.7 GB log that meant every one of its 50,000 rows.

`mcc-compact-log` (legacy alias `fcc-compact-log`) rewrites them in place:

```bash
# stop the server first, or the final vacuum cannot reclaim the space
mcc-compact-log
```

Measured on a copy of that 1.7 GB database: **1.73 GB → 0.29 GB in 4.9 minutes**, and all 49,934 bodies verified byte-identical against the original afterwards. It is safe to interrupt — each batch commits on its own and a row is converted only after its body is stored, so a kill leaves a consistent database with the work merely unfinished. Running it again resumes.

It also **deduplicates prompts**. The prompt is 98% of the stored bytes and 35.3% of those bytes are exact repeats — a retry or a parallel subagent re-sends the same context — so it is stored in its own shared blob, apart from the reply, reasoning and tool calls that differ every time. Keying on the whole body instead deduplicated 1.4%; keying on the prompt alone removed **29.9%** of an already-compressed real log (299 MB → 209 MB, 35,461 distinct prompts across 50,460 requests).

#### No traffic, or no server?

A flat stretch in **Requests over time** means one of two very different things, and the chart alone cannot tell you which. The server records when it was actually running, so a line under the chart says whether a server covered the range or how much of it had none. Pick a time range to see it — over "all time" there is no bounded window to measure against.

Uptime is only recorded from v4.44.0 onwards, so earlier periods report nothing rather than claiming downtime that was never measured. Brief gaps from a restart are ignored; the threshold scales with the range you are looking at.

<a id="tutorial-read-the-request-detail"></a>

### Tutorial: read the request detail

The detail dialog answers the question "what did MCC actually put on the wire, and which key sent it?" — as opposed to what your configuration says it should have. Every field below is a measurement, not a restatement of settings.

<div align="center">
  <img src="../assets/admin-request-detail.png" alt="Request detail dialog showing the per-attempt wire pane" width="860">
</div>

**1. Open Analytics and find the request.** Filter or search (search reaches the reasoning text and tool arguments too), then press **View** on the row. Since 6.13.0 every filter applies itself — the selects the moment you change one, the text boxes a short pause after you stop typing, and **Clear filters** puts everything back including the default below. **Apply** is still there for when you would rather press it.

**Local answers** defaults to **Hide**, so the table, the cards, the charts and the breakdowns show requests that actually went to a provider. Requests MCC answered itself — title-generation skips, probe replies, suggestion-mode skips — are hidden until you switch it to **Show** (everything, as before 6.13.0) or **Only** (nothing else). The choice is remembered across refreshes. Rows whose provider is genuinely unknown are not local answers and stay visible under Hide, and the **All time** rollup and the export window ignore this filter.

**Harness** names the coding agent that sent the request. Since 6.37.0 every row records it, the
table shows it as a chip, the detail dialog spells it out, there is a **Harness** filter beside
Provider and Model, and Analytics carries a **Requests by harness** breakdown with counts, error
rate, tokens and average latency. The **Coding agents** page shows `Requests (7d)` on each card from
the same data. MCC works it out two ways, and the detail dialog tells you which:

| Detail dialog says | How MCC knows |
| --- | --- |
| `OpenCode 1.18.26 (explicit header)` | MCC's own launcher wrote `x-mcc-harness` into the config it generates for that CLI. Exact, not a guess |
| `Claude Code 2.1.258 (from user-agent)` | inferred from the `user-agent` the client sent |
| `Unknown (no client identification)` | the client sent nothing MCC recognises, or nothing at all |

Eleven of the sixteen agents can carry the explicit header, because their configuration has
somewhere to put one: Claude Code and Gemini CLI through an environment variable, Codex through a
`-c` assignment, Pi through the provider its bundled extension registers, and OpenCode, OpenCode 2,
Kilo, Command Code, Qwen Code, Crush and Cline through the provider document MCC generates for them.
Kimi Code, Aider, Droid and Goose publish no header hook MCC could verify, and Antigravity is not
launched by MCC at all, so those five are identified by user-agent alone.

Two caveats worth knowing before you read the numbers:

- **`Unknown` is large on an old log, and that is history, not a bug.** MCC only started recording
  inbound headers in 5.36.0, so any request written before that has nothing to classify. On the
  reference log that is 146,658 of 272,132 rows. Requests written from 6.37.0 on are always
  attributed to something, even if that something is `Script or curl`.
- **Qwen Code and Pi send Claude Code's user-agent.** Both build `claude-cli/<their own version>`
  on their Anthropic path, so requests from them before 6.37.0 are counted as Claude Code. The
  explicit header separates them from 6.37.0 onward. `Claude Agent SDK` is broken out separately
  from `Claude Code` on purpose — it is a different program, and on the reference log it is the
  busiest client on the box.

**A model that never appears in Analytics never reached the proxy.** The request log records what
MCC was asked to serve; if a model you tried is missing entirely — no rows, not even failed ones —
the agent never sent MCC a request for it. Look at the agent's own configuration and base URL, not
at Analytics. This is different from a request that arrived and failed, which is always logged with
its error.


**2. Read the chain first.** One entry per attempt: outcome badge, model ref, how long it took, and — this is the 6.4.0 addition — **the credential that served that attempt**. Three things it can say:

| Shown | Means |
| --- | --- |
| a key label (`nvap…8f21`) | that attempt used that key. Attempts in one request can name different keys |
| **`no key available`** | every credential in the pool was benched; the attempt never reached a key at all. Since 6.19.0 this splits in two: the whole pool in cooldown, or every key rate-limited **for this model** while its other models still serve |
| a dash | no credential was recorded — not measured, not "keyless" |
| a **ladder headline** (`15 tries · 12×429, 3×502 · 3 keys · 96s sleeping`) | 6.12.0: how many times that attempt actually knocked, and what it met |

**3. Read the ladder.** An attempt is not one request to the upstream. It is up to five tries per credential, across every credential the pool hands it, with MCC's own exponential backoff between them — and before 6.12.0 the database recorded exactly one status for the whole thing. Under a failed attempt you now get four things:

- **The headline**, beside the duration: `15 tries · 12×429, 3×502 · 3 keys · 96s sleeping`. If the sleeping figure is most of the duration, the model was never the slow part.
- **The root-cause line**, stored with the row rather than composed in the browser, so the modal and all four export formats say the same sentence:

  > `3 keys × 5 tries: 12×429, 3×502 — 96s of the 107s were MCC backoff sleeps; keys 0 and 1 benched 60s on 429 (no Retry-After); key 2 not charged (502 is not credential-shaped)`

  That is the real `req_f3b018…`, which the database had recorded as a single `upstream` failure with `HTTP 502` on key 0. The 502 was the last thing that happened, not the thing that went wrong.
- **Show N upstream tries** — one row per try: `#7 · key 1 nvap…8f21 · 429 · 410ms · waited 4900ms · retry-after 12s`. A term that was not measured is omitted, never printed as `0`; a redacted excerpt of the upstream's own body sits under each try that had one, capped by `REQUEST_LOG_LADDER_BODY_MAX_CHARS` (800).
- **The credential decisions** — one line per key the attempt touched, saying whether the pool charged it and why: `key 0 nvap…8f21 — benched 60s (rate_limit): 429, no Retry-After -- operator cooldown 60s`, or `key 2 nvap…c4d0 — health unchanged: 502 is not credential-shaped`. The bench duration is read back out of the rotation engine that set it, never recomputed.

A **timeout** attempt gets a different sentence when most of its duration was spent waiting:

> `deadline reached after 148s of backoff — the model never received an accepted request: 4×429, 1×502`

That is the case the old row got actively wrong. It said *"Provider 'x' produced no output within 120s"*, which sends you to look at the model — when the model was never handed an accepted request at all.

An attempt with a single try shows no ladder: nothing was hidden, so nothing is added. **Rows written before 6.12.0 show no ladder either — that is "not measured", not "there were no retries".**


**4. Expand an attempt's wire pane.** The headline line is the numbers people open this dialog to check, e.g. `max_tokens 131,072 · raised from 64,000 for reasoning · 59 tools · temp 0.7`. That second term appears only when the allowance was widened because the attempt was going to think; the full modal line reads *max_tokens raised from 64,000 to 131,072 for reasoning*, and it is backed by a per-attempt `params.output_widened_from`.

**5. Then the parameter block, which is the important one.** Every parameter MCC sent is listed there — top-level keys, and each provider-specific `extra_body` key under an `extra_body.<name>` row — **above** the message structure, and **never truncated**. A knob MCC learned to send but this pane had never heard of (`min_p`, `tool_choice`, `response_format`) shows up whole rather than falling off the end. A parameter that was not sent has no row: absence is the finding, so it is shown as absence and not as a dash. Anything that looks like a credential by name or shape reads `<redacted>`.

**6. Below it, the stored body — and why it may be shorter than you sent.** The body is stored content-first: the knobs are written whole first, and only `messages` and `tools` degrade, to counts and names, under `REQUEST_LOG_WIRE_BODY_MAX_CHARS` (8,000 by default). The note says exactly that: *Message and tool structure reduced to counts at 8,000 of 214,317 characters. Every parameter is stored whole and shown above.* The stored body always parses as JSON. Before 6.4.0 the cap cut the body alphabetically, so a Claude Code request with ~59 tools spent its whole budget inside `tools` and `temperature`, `top_p` and `reasoning_effort` survived in **0 of 212** truncated bodies measured in one day.

**7. Read the reasoning state on the same summary line.** Three honest states, and none of them is a hidden pane:

- **`reasoning sent: high`** — a reasoning instruction went on the wire, with the value it carried.
- **`no reasoning instruction sent (model default applies)`** — nothing was sent, and that is a correct outcome, not a fault: the host has no field this model's capability could be expressed through, so the model's own default stands.
- **`reasoning not measured`** — this provider has no instrumented commit boundary (Vertex, permanently), or the attempt was never sent.

**8. Take the contradiction badge seriously, and only when it appears.** *gating asked for reasoning; nothing was sent* means the resolved policy chose a value and the body carried none — a real gap between policy and encoder. It is keyed on the stored `reasoning_adaptation_kind` column, never on message text, and it fires for exactly two kinds: **`clamped`** (your rung was moved to the nearest one the host can spell) and **`substituted`** (a different expression of the same intent went instead). **`dropped` stopped badging at 6.6.0** — it had covered two opposite situations and was flagging correct behaviour as a defect — and `nothing_sent` never badges. Rows written **before 6.4.0** badge nothing, because the column did not exist: not measured is not a finding.

**9. Finally, the thinking pane.** `thinking_chars` follows one convention across the whole surface: **`0` is a measurement** — a completed stream that returned no reasoning — and **NULL renders as "Not measured"**. Before 6.8.0 the two were folded together, so "the model thought about nothing" and "nobody was counting" looked identical.

> **One gotcha worth knowing.** `output_widened_from` is deliberately a plain per-attempt parameter and *not* a reasoning adaptation kind, so widening never badges and never appears in the adaptation severity table. Relatedly, the request-level adaptation line can still name one model while a fallback actually answered — read the per-attempt panes when the chain has more than one entry.

### The Token Optimizer page

**Admin UI → Token Optimizer** answers one question from your own request log: what never reached a provider at all? Nothing on this page is switched on for you.

Some requests are answered inside the proxy by a **local rule** — MCC replies and no provider is ever contacted. Those requests show in the request table as **answered locally · <rule>**, not as provider `(unknown)` the way they read before 5.48.0, and you can filter the table by that value to see only them — or use the **Local answers** filter on Analytics, which hides them all by default and shows only them on **Only**. Because no provider served them, they record no provider, and the tokens they saved are counted from the real request rather than assumed.

The page has four panels:

- **Ledger** — "Tokens never sent": prompt tokens no provider ever received. This is not a bill estimate. What a provider would have charged for the reply cannot be known, and MCC does not guess at it.
- **Local rules** — how often each rule actually fired.
- **Candidates** — recurring request shapes that no rule covers yet, ranked by the tokens they really cost. Press **Scan the log** to produce them. The scan is on demand only: it never runs on a schedule or when the page loads, it reads nothing until you ask, and it changes nothing about how any request is answered. Ask it for more rows than it will scan and it refuses outright instead of quietly sampling and presenting the sample as the whole picture.
- **Cache effectiveness** — prompt-cache hit rate per provider. This is the biggest lever on the page and the optimizer does not control it. A dash means the provider never reported the figure, which is not the same as reporting zero.

Three local rules ship today, each with its own kill switch:

- **Title generation** (`ENABLE_TITLE_GENERATION_SKIP`) — Claude Code's request for a short conversation title.
- **Suggestion mode** (`ENABLE_SUGGESTION_MODE_SKIP`) — a `[SUGGESTION MODE:` turn, which expects no model output at all.
- **Model routing probe** (`ENABLE_PROBE_AUTO_RESPONSE`) — an agent harness's startup reachability check, sent before a run to catch a proxy quietly serving a different model than the one asked for. It is unmistakable: a single `Say OK` user turn, no system text, no tools, not streaming, and `max_tokens` of 16. MCC answers it in milliseconds instead of spending a real upstream call, and the reply names the model that *would* have answered — the first model on the route your fallback health registry has not benched, which is not always the primary — so the probe still detects a substitution truthfully. Set it to `false` if you would rather the probe reach your provider.

The page also shows RTK's measured savings and warns you if the RTK binary installed on your machine has drifted from the version MCC pins.

#### Tool-result trimming (off, and worth leaving off)

Claude Code re-sends the entire conversation every turn, so one big file read is paid for again on every turn after it. MCC can shorten large `Read`, `Grep` and `Glob` results on their way to the model. The controls moved here from the **Limits** tab in 5.48.0.

**It is off by default and this guide is not recommending you turn it on.** It was measured, and it lost. Over a 24-turn session, at the shipped setting, trimming cost **10.9% more fresh input tokens than not trimming at all** — rewriting bytes in the middle of a prompt throws away the provider's prefix cache, and the cache is worth more than the removed text. Turning it on part-way through a conversation costs a near-total cache miss on that turn (a 3.8% hit rate). It only starts to pay if your baseline cache hit rate is **below about 90.9%**, and a well-behaved provider usually sits above that. The full measurement is in `.env.example` and in the source docstring of `core/anthropic/tool_result_trimming.py`.

What it will not do:

- Nothing happens unless you change **two** things: `ENABLE_TOOL_RESULT_TRIMMING` is `false`, and each per-tool rule is separately `off`.
- It never touches `Bash` output.
- It never trims invisibly — every cut carries a note saying MCC made it, how much is missing, and that the model must not describe what it did not see.
- Anything it does not completely understand is passed through untouched.

Each rule has three states, and the middle one is why you would look at this at all: `off`, **`observe`**, and `on`. **`observe` measures what a rule would have removed from your real traffic without changing a single byte on the wire.** That is the way to find out whether trimming would help you: run `observe`, compare against your own cache hit rate on this page, and only then decide. The remaining settings — the size threshold below which nothing is touched, how much of the head and tail survive, and how many recent results are exempt — are documented in the Admin UI and in `.env.example`.

### Web search analytics

The Web Search tab has its own analytics with an important distinction made explicit:

- **Logical searches** — one per `web_search` call.
- **Provider attempts** — one per try. A fallback produces several attempts for one search.

The two are shown in separate tables so the numbers reconcile.

```bash
WEBSEARCH_LOG_ENABLED=true
WEBSEARCH_LOG_MAX_ROWS=50000
WEBSEARCH_LOG_CAPTURE_CONTENT=true      # false = lengths and hashes only
WEBSEARCH_LOG_CONTENT_MAX_CHARS=2000000 # cap per input/output JSON payload
```

> **Privacy.** Search content routinely includes private queries, result URLs and page text. `WEBSEARCH_LOG_CAPTURE_CONTENT=false` withholds the captured payloads **and the query text itself**, keeping only lengths and SHA-256 hashes.
>
> API keys are never written to either store — only masked `first4…last4` labels. Proxy credentials are stripped from recorded URLs.

---

## 11. Multi-key rotation

Both model and web search providers accept several keys in one variable:

```bash
EXA_API_KEY="key-a,key-b,key-c"
EXA_API_KEY_ROTATION=failover
```

| Policy | Behaviour |
| --- | --- |
| `single` | Only the first key. Default with one key. |
| `round_robin` | Even spread across healthy keys. |
| `least_used` | Prefers the key with fewest requests. |
| `failover` | First healthy key; move on when it fails. Default with several keys. |

### Health tracking

Each key carries its own state, and only three things change it. A **401/403** locks the key out on an escalating ladder (`CREDENTIAL_LOCKOUT_TIERS`). A **429** benches it for exactly as long as the provider asked, and only for the **model** that was rate-limited (see `CREDENTIAL_MODEL_BENCH_ESCALATION` below). **Exhausted credits** named in the provider's own words bench the whole key for `RATE_LIMIT_COOLDOWN_SECONDS` (6.34.0). Nothing else does — a timeout, a 5xx, a `410 model gone`, an ordinary 400 or a dropped connection leaves every key exactly as it was, because the same keys serve every model in your chain and none of those failures is the key's doing. When a model will not answer, the fallback chain moves to the next **model**.

A **rate-limited key is benched for exactly as long as the provider says** — parsed from `Retry-After`, `retry-after-ms` or `x-ratelimit-reset-*` — rather than an invented fixed delay. A key that resets in one second isn't idled for a minute, and one that needs an hour isn't hammered.

Since 6.19.0 that bench is also scoped to the **model** it happened on. Gateways that front many models rate-limit them separately: measured on NVIDIA NIM on 2026-08-31, `moonshotai/kimi-k3` returned 429 on all three keys inside 0.1 s while `nvidia/nemotron-3-ultra` and a MiniMax model answered on those same keys in the same second — and NIM sends no `Retry-After` at all. Charging the whole key for that took **every** NIM model off the route for a full minute. Now the pair is benched, the key stays `HEALTHY`, and the key itself is benched only once `CREDENTIAL_MODEL_BENCH_ESCALATION` (default 2) different models are limited on it at once — which is what a genuinely key-wide limit looks like, one extra 429 later.

Per-key state, usage and health are visible in the Admin UI, including which key served which request; the ladder and the no-header cooldown are the **Credential health** card on [Limits and resilience](#12-limits-and-resilience).

<a id="tutorial-why-my-key-was-benched"></a>

### Tutorial: why my key was benched

A key that is out of rotation and a model that is failing look the same from your agent's side, and until 6.0.0 MCC largely conflated them: on one live three-key pool, healthy keys were taken out of service **1,529 times in a single day** by failures none of them had caused — a `410 model gone` on one model ref, a `400 top_p immutable`, a first-token timeout on a slow route. Rotation and key health are now separate questions. This is how to tell which one you are looking at.

<div align="center">
  <img src="../assets/admin-credential-health.png" alt="Credential health card and per-key state badges" width="860">
</div>

**1. Look at the key pool.** Providers → the provider's card → **Configure**. Every key in the pool carries its own state badge, and hovering it gives the rest: `LOCKED_OUT — back in 42m — 1,208 requests, 3 failures`.

| Badge | What put it there |
| --- | --- |
| `HEALTHY` | in rotation |
| `HEALTHY` with a model line under it | a 429 on **one model** — that model is benched on this key for a stated time, everything else on the key still serves |
| `COOLDOWN` | a 429 that cost the whole key: several models limited on it at once, or `CREDENTIAL_MODEL_BENCH_ESCALATION=1` |
| `LOCKED_OUT` | a 401 or 403 — benched on the escalating ladder |

**2. Confirm it against a real request.** Analytics → **View** on a failing request → the chain panel names the key per attempt (see [the previous tutorial](#tutorial-read-the-request-detail)). If every entry reads **`no key available`**, nothing was attempted upstream. The error says which of the two reasons applies: *All API keys for this provider are in cooldown* is a credential problem, while *All API keys for this provider are rate-limited for `<model>`* is a **model** problem on a healthy pool — try another model on that same provider. If the attempts name keys and still fail, it is a *model* problem and no key is at fault.

**3. Read the ladder under the attempt.** Since 6.12.0 the chain panel names every upstream try, and one line per credential saying whether the pool charged it. If the ladder shows `12×429` before a `502`, the 502 is not the story — the pool was throttled and the last key simply happened to fail differently.

**4. Match the failure to what it costs.** Exactly two signals charge a key. Everything else is free:

| What the provider returned | The key | The request |
| --- | --- | --- |
| **401 / 403** | steps the `CREDENTIAL_LOCKOUT_TIERS` ladder — **300 s, then 3,600 s, then 86,400 s** (5 min → 1 h → 24 h), one step per consecutive rejection, staying at the last entry | rotates to the next key |
| **429** | the **(key, model) pair** is benched for **exactly the provider's `Retry-After`** — parsed from `Retry-After`, `retry-after-ms` or `x-ratelimit-reset-*` — or `RATE_LIMIT_COOLDOWN_SECONDS` (60 s) when the host publishes no header, capped at one hour either way. The key itself is benched only once `CREDENTIAL_MODEL_BENCH_ESCALATION` different models hold a live bench on it | rotates to the next key |
| connection error / transport fault | nothing | rotates to the next key |
| **timeout, 5xx, `410 model gone`, "overloaded", 400, context overflow** | **nothing at all** | moves to the next **model** in the chain |

The rule behind the table is "judge a key only on signals about the key". The same keys serve every model in your chain, so a model that is gone, overloaded, or slow says nothing about the credential holding the request.

**5. Act on what you found.**

- **`LOCKED_OUT`, one key, others healthy** — that key is wrong, expired or revoked. Remove it from the pool; the ladder is not going to heal a dead key, and by the third rejection it is out for a day.
- **`LOCKED_OUT`, every key** — it is not the keys. Check that the provider is the one the key belongs to, and that your account still has API access.
- **`COOLDOWN` constantly, on every key** — you are over the provider's rate limit, not short of keys. Lower `PROVIDER_RATE_LIMIT` / `PROVIDER_MAX_CONCURRENCY` on **Limits & Resilience → Retries & throughput** rather than adding a fourth key that will cool down alongside the other three.
- **All keys `HEALTHY`, requests still failing** — nothing is wrong with your credentials. Read the chain panel's error kind: this is a model, deadline or benching question, and it lives on [Limits and resilience](#12-limits-and-resilience).

> **The deliberate gap.** A key that fails with a 5xx or a transport fault on *every single request* is never benched — rotation tries it once per request and the chain absorbs the cost. That is the trade MCC made knowingly: the failure classes that could identify such a key were the same ones emptying healthy pools by the thousand. A 401 or 403 still locks it out, which is how a genuinely dead key gets caught.
>
> If your `~/.mcc/.env` still carries a `CREDENTIAL_CIRCUIT_THRESHOLD=` line, it configures nothing — the breaker it belonged to was removed in 6.0.0. The line is ignored rather than fatal; delete it when convenient.

### The RTK token optimizer

RTK (the Rust Token Killer, v0.45.0) is an optional third-party binary that filters noisy terminal output before it reaches the model — trimming the token cost of long, chatty agent sessions without changing what the agent does. MCC manages the binary and its per-agent hooks through one command, `mcc-rtk` (legacy alias `fcc-rtk`):

```bash
mcc-rtk status              # installed binary + enabled agents
mcc-rtk enable claude,pi    # install the hook for these agents
mcc-rtk disable codex       # remove the hook for one agent
mcc-rtk uninstall           # disable every agent and remove the binary
mcc-rtk apply               # re-apply the stored state to the machine
```

Enablement is per agent — `claude`, `codex`, and `pi` are each toggled independently. On first enable MCC downloads the pinned RTK release, verifies its SHA-256, installs it under `~/.local/bin`, and patches the agent's own config with telemetry disabled. Desired state lives in `~/.mcc/rtk.json`; `mcc-rtk apply` reconciles the machine against that stored state after any drift.

The same controls live in the dashboard under the **Token optimizer** card and in the `mcc-desktop` tray's **Token optimizer** submenu, so the three surfaces stay in sync.

MCC reads RTK's own `rtk gain` report and shows the resulting savings on the [Token Optimizer page](#the-token-optimizer-page), and tells you when the RTK binary installed on your machine is no longer the version MCC pins.

**RTK telemetry is off when MCC enables it, and MCC cannot turn it on.** MCC patches each agent's config with telemetry disabled and additionally forces RTK's telemetry opt-out environment variable on every invocation, which also short-circuits RTK's own consent prompt. Enabling RTK through MCC therefore cannot opt you into RTK telemetry.

---

## 12. Limits and resilience

<div align="center">
  <img src="../assets/admin-limits.png" alt="Limits and resilience configuration" width="860">
</div>

**Admin UI → Limits & Resilience** holds every setting that decides how long MCC waits, how hard it retries, and when it stops. Six cards — **Budgets**, **Deadlines**, **Chain benching**, **Retries & throughput**, **Credential health**, **Diagnostics** — each stating in one line what it decides, reachable from the sticky section rail down the side of the page. It replaced a single flat grid of 37 fields, two thirds of which were only reachable behind a *Show advanced* toggle; the cost of the split is a long page, which is why the rail follows you down it. Every numeric field carries its accepted range on its own line under the input, so you can see what a box will take without reading the help text.

### Output & thinking budgets

How large one answer may be. MCC sizes `max_tokens` from the routed model's own published limit; these settings only cover what the model cannot answer for itself — the budget used when nobody publishes a limit, the absolute ceiling, the tokens held back so a large output limit cannot swallow its own context window, the smallest bounded budget that reserve may produce, and the answer reserve kept back while extended thinking is on.

**A request that is going to think starts from the model's maximum, not from what the client asked for.** Thinking tokens and the answer are spent from one `max_tokens`, so a client that sized the number for an answer unknowingly sized the thinking as well — and it is the model, not the client, that knows how much it can emit. The ceiling, the context reserve and the model's own limit then clamp that exactly as they clamp any other ask.

**The ceiling ships set at 131,072 for that reason.** Some hosts — OpenAI and Azure style limiters — reserve `max_tokens` against your rate-limit bucket *before* generating anything, so an unbounded thinking turn on a 262,144-output model can 429 a request that would otherwise have been served. The head applies uniformly, reasoning or not; it never raises a model above its own published limit. Set it to **0** to lift it entirely and let every model's own limit stand. Leaving the box empty now means "use the default", not "no ceiling" — that is the one upgrade edge worth knowing about.

### Deadlines

When to stop waiting. The first-token deadline, the whole-request budget, the stall deadline for a stream that started and then went quiet, how long a model may think before the chain moves on, whether reasoning is held back so a thinking model can still be replaced, the commit holdback that keeps a recovery invisible, the three transport timeouts underneath all of it, and how long a closing process gives in-flight requests to drain.

**All five deadlines ship at `0` — no limit — since 6.16.0, and that has one consequence worth stating plainly: with the shipped zeros MCC never ends a silent or stalled upstream on its own. The fallback chain moves only on an error the provider actually returns.** A model that thinks for forty minutes is left to think. A stream that produces two sentences and then goes silent forever stays open until the transport read timeout ends it (`HTTP_READ_TIMEOUT`, 300 s, applied per read rather than per request) or the client disconnects. Nothing in MCC will step in first.

That is a deliberate reversal. Through 6.15.0 these shipped as measured numbers (180 s first token, 600 s budget, 180 s floor, 180 s stall, 450 s thinking) and the failure they produced was the worse one: a reasoning model doing real work was killed mid-thought, the client got `Provider 'x' produced only reasoning for 450s without answering`, and nothing in that sentence said which knob had done it or where it lived. MCC is a system for the operator who runs it, so the shipped value is the one that decides nothing.

**Three settings give you time-based failover back, and they measure different things:**

| Set this | To bound | What happens when it fires |
| --- | --- | --- |
| `FALLBACK_FIRST_TOKEN_TIMEOUT` | silence *before* any output reaches the client | the next model on the chain takes over — the client never sees the handover. **This is the only deadline that produces a failover.** |
| `FALLBACK_STALL_TIMEOUT` | silence *after* output started, measured from the last chunk that moved the answer forward | the request ends. No model can replace a stream the reader is already looking at; with `FALLBACK_END_CLEANLY_AFTER_COMMIT` on it ends as a truncated message rather than an error. |
| `FALLBACK_TOTAL_TIMEOUT` | the whole request, across every attempt, retry and recovery | the request ends, wherever it had got to. |
| `FALLBACK_REASONING_ANSWER_TIMEOUT` | thinking that never becomes an answer, once the provider says it is holding reasoning back | the chain moves on, provided `FALLBACK_ON_REASONING_ONLY` is on — that is what keeps the attempt abandonable. |
| `STREAM_COMMIT_HOLDBACK_SECONDS` + `STREAM_COMMIT_HOLDBACK_CHARS` | how much has to arrive before output is released to you at all | nothing is ended; this is the width of the window in which a failure is still invisible and the next model can start over with nothing shown. Both conditions must be met, or the stream must end. |
| `FALLBACK_ATTEMPT_SHARE_FLOOR` | nothing on its own — it is the smallest slice of `FALLBACK_TOTAL_TIMEOUT` the chain-side division may hand one model | see the arithmetic below. Moot while the total budget is `0`. |

The **Deadlines calculator** on this page turns whatever you set into the number each model on each of your routes actually gets. At the shipped zeros every row reads **no limit**, and the headline names `HTTP_READ_TIMEOUT` as the only thing left that ends a silent model.

**An install that already sets any of these keys keeps its own values.** Upgrading rewrites nothing; the zeros are what a key you never set falls back to.

**Every error one of these limits raises now names its own knob.** A request MCC ended reaches your client as, for example, `Provider 'open_router/qwen/qwen3-max' produced no output within 300s. (FALLBACK_FIRST_TOKEN_TIMEOUT -- change it on the dashboard under Limits & Resilience -> Deadlines)`. The hint names the limit that *actually* ended the attempt: a silent model cut short by its slice of the request budget rather than by the first-token deadline names `FALLBACK_ATTEMPT_SHARE_FLOOR` instead, because raising the first-token box would not have changed anything. The same sentence appears on the attempt row in the request detail, and on the `All API keys for this provider are in cooldown` error, which names `RATE_LIMIT_COOLDOWN_SECONDS` and the **Credential health** card.

**A model's first-token allowance is the smaller of the deadline and its share of the budget — and that share has a floor.** Each attempt gets an equal share of whatever is left of the total budget, counting itself and every model still behind it on the chain; that share is then raised to `FALLBACK_ATTEMPT_SHARE_FLOOR` if it came out lower (never above what is actually left), and what is applied is the smaller of the result and the first-token deadline.

Without the floor the share alone decides: with 600s total and a 120s deadline, a ten-model route gives the first model `min(120, 600 ÷ 10)` = **60s**, and the 120 in the box never applies to that route at all. That is what produced log lines like `produced no first token after 74.9494s` on an eight-model chain — a number that appeared nowhere in the configuration. Set 600s total, 180s first token and a 180s floor and the same route gives every silent model the full **180s**: `min(180, max(600 ÷ 10, 180))`.

The floor is **chain-side**: it bounds each model's first-token allowance, never a retry of the same model. And it buys its honesty with the models behind it — ten models at a 180s floor is 1,800s of demand against a 600s budget, so only the first three silent models can use the whole floor and the ones after them get whatever is left, then nothing. That is the operator's trade to make. `FALLBACK_ATTEMPT_SHARE_FLOOR=0` (the shipped value) is the pure equal-share, and while `FALLBACK_TOTAL_TIMEOUT` is `0` the whole division is moot: there is no budget to divide, so nothing can undercut the first-token deadline. The Deadlines calculator on this page computes both numbers for your own chains and warns when the floor cannot fit.

**A dead stream is now continued on the next model (6.18.0).** With `FALLBACK_RESUME_AFTER_COMMIT` on (the default), a model that dies part-way through an answer no longer only *ends* the message: the next model on the route is given the words already on your screen, asked to carry on from them, and its output is spliced into the same message. One `message_start`, one text block, one ending — there is no visible seam, on purpose. The model change is recorded in the request detail instead: the stalled attempt keeps its own failure row, and the attempt that finished says *continued here after `<model>` stalled at N chars*. It uses the same chain as any other fallback — benched models skipped, `FALLBACK_SKIP_KINDS` still ending a route, the same request budget, no new retry layer. Continuation is not reliable on every model: many answer nothing at all, and a model that starts the answer over is detected and thrown away rather than printed twice. Every one of those outcomes falls through to the truncated message below, never to an error, which is why it ships on. A half-written tool call is never continued. The first characters of a continuation are held back until it has proved it is continuing rather than restarting, so the rescue costs a short pause before the answer resumes.

**A stream that dies after it started answering now ends as a message, not an error (6.15.0).** The chain commits the moment real text reaches you: from there no other model can take over, because your client has already printed the first model's words. Every failure past that point — the stall deadline, the total budget, a mid-stream 5xx, a dropped connection — used to arrive as an API error underneath a half-written answer, and the turn was dead. With `FALLBACK_END_CLEANLY_AFTER_COMMIT` on (the default) the message is *ended* instead: the open block is closed and the client is told the answer was cut short, so the session continues with a short but complete reply. Nothing about when the stream is stopped changed — only what your client is handed. The answer is genuinely incomplete: the request detail shows the attempt still `failed`, with its real cause, alongside `ended early after N chars`. One case still errors — a stream that stopped halfway through a tool call's arguments, which cannot be completed honestly. Set it `false` for the old error.

The Deadlines card works this out for you. It shows one row per configured route — Default, Fable, Opus, Sonnet, Haiku, Vision — with the arithmetic for that route's chain length, names the floor in the formula when the floor is what decides, and warns either that the floor cannot fit the budget (naming how many silent models it does cover) or that the total budget cannot honour the first-token deadline (naming the budget that would). Routes with no model of their own are left out, because they run on `MODEL` and its chain.

It is a model of the executor, not the executor. It does not know about time already spent earlier in the request, about **Retry primary once** adding an attempt, about a benched model shortening the chain, or about the reasoning path taking over. The card says so where it sits.

<div align="center">
  <img src="../assets/admin-limits-calculator.png" alt="The per-route deadline calculator on the Deadlines card" width="860">
</div>

It also catches two settings that quietly undo a deadline: an `HTTP_READ_TIMEOUT` below the per-model allowance (a slow model then produces a transport error instead of a clean handover to the next one), and a graceful-shutdown window shorter than the total budget (a reload force-drops requests before the budget expires).

**Known gap.** A stream that thinks and then ends with an empty visible answer — `finish_reason=length` after the thinking consumed the allowance — is not rescued by the fallback chain. **Fall back when a model only thinks** only covers a *deadline* reached while a stream is still open, so a stream that ended on time, with output, never reaches it. Raise `MAX_OUTPUT_TOKENS_CEILING`, or lower the reasoning tier, if you see it.

### Chain benching

When to stop trying a model. **Bench failures** is the master switch: turn it off and every other control in the card is inert, and a failing model is tried again on every request.

The mode you are not using stays on screen but disabled, with a note saying which — *Not used while eject mode is legacy*, *Not used while benching is off* — rather than only being dimmed. Disabled fields are skipped by the change tracker, so a mode's unused knobs cannot be saved by accident.

With it on, **Eject mode** picks the arithmetic. `rate_based` (the default) benches a model when at least the failure-rate threshold of its last N requests failed, with a minimum sample count so one bad request on a quiet model cannot trip it. `legacy` benches after a number of *consecutive* failures instead. Each mode ignores the other's settings. Both share how long a benched model stays out of routing, whether the primary gets one more chance before the chain is used, and the shortest remaining rate-limit cooldown that makes stepping over a model worth the chain slot it costs.

Benching never empties a chain: if every model on a route is benched they are tried in order anyway. Which failures abort the chain instead of falling through is a routing decision, not a resilience one, so `FALLBACK_SKIP_KINDS` stays on **Model Config**; the card links across to it.

### Retries & throughput

How hard one model is tried before the chain is used at all: the retries on a 5xx or a dropped connection (a 429 is routed around instead — see **Credential health**), the attempts a provider makes on its own before routing ever sees the failure, the recovery attempts after output has started and the connection dropped, and the exponential backoff between them — first wait, ceiling, and the random jitter that stops several clients retrying in lockstep. The same card carries the client-side pace: requests per window, the window, and how many streams one provider may have open at once.

### Credential health

What one key's failures cost it, and it is a short list. A **401 or 403** walks the lockout ladder — `CREDENTIAL_LOCKOUT_TIERS`, five minutes then an hour then a day by default, one step per consecutive rejection and staying at the last entry. A **429** benches the key for exactly as long as the provider asked in its `Retry-After`, or for `RATE_LIMIT_COOLDOWN_SECONDS` when it sends no header — and only for the model it happened on, until `CREDENTIAL_MODEL_BENCH_ESCALATION` (default 2) different models are limited on that key at the same time, which is when the limit is the key's rather than the model's. A **timeout or a 5xx costs a key nothing at all**, because the same keys serve every model in your chain and neither failure is the key's doing.

Since 6.20.0 that 429 also stops costing the *request* anything. `RATE_LIMIT_ROUTES_AROUND_MODEL` (default on) means the pair is benched and the request goes straight to the next model on the **same provider** — same pool, same key — because that is where the evidence points. Nothing sleeps between the two, no provider-wide block is installed, and no key is rotated. When your chain holds no other model on that provider, MCC asks one 16-token question of a model you already configured there: a `200` says the key is fine and only the model is limited, a `429` says the limit is the key's after all and it is benched and rotated exactly as before. That probe appears in the request-detail ladder as its own row, tagged `probe`, and never as a request in your analytics. Turn the setting off to get the old behaviour back, sleeps and all.

### Tutorial: why my request took 57 seconds

Open the request in **Analytics → Requests** and expand the attempt. The ladder under it is the whole story: how many times MCC knocked, what each knock met, which key carried it, and — the number that matters here — how much of the attempt was MCC asleep rather than waiting on the model. A real one read *"3 keys × 5 tries: 14×429, 1×502 — 50s of the 57s were MCC backoff sleeps; keys 0, 1 and 2 benched 60s for moonshotai/kimi-k3"*. Fifteen knocks, six seconds of actual upstream time, and a healthy model on the same three keys sitting one chain slot away that was never asked. If you see a line like that on an install running 6.20.0 or later, check that **Credential health → Route around a rate-limited model** is on, and that the model that refused actually has a sibling configured on the same provider.

### Diagnostics

The logging flags, and the log level that used to sit on its own. Leave them off unless you are chasing something: they are verbose by design.

Two cards that used to live here now sit where you see their effect. The `REQUEST_LOG_*` retention settings are at the bottom of **Analytics**, and the `DESKTOP_*` settings are on **Providers**, beside the live desktop panel.

---

## 13. Updating

<div align="center">
  <img src="../assets/admin-version.png" alt="Version panel" width="860">
</div>

The dashboard shows your running version, checks the release feed (cached for six hours), and announces new releases with **the release notes inline** — expand *What changed* to decide whether an update matters to you.

<div align="center">
  <img src="../assets/admin-update-banner.png" alt="Update available banner" width="860">
</div>

**Update now** downloads the release wheel, verifies its SHA-256 against the digest GitHub publishes for that asset, and installs it with `uv`. A checksum mismatch aborts. Extras you originally installed — voice support, for instance — are detected and preserved.

**Upgrading never restarts the server.** A running process keeps serving the code it already loaded, so an upgrade can't drop an in-flight stream. You get a *restart required* banner and restart when convenient.

### Windows: the install is deferred

Windows holds a running executable and its loaded DLLs open, so the environment **cannot** be replaced underneath a live process — attempting it fails partway and leaves a broken install.

So on Windows, **Update now** downloads and verifies the wheel, then hands it to a background helper that waits for the server to exit and installs it then. You'll see:

> *Update staged — stop the server to finish installing*

Stop `mcc-server`, the update applies itself, start it again on the new version. **Your working install is untouched until that moment**, so a failed update can't strand you. If the deferred install does fail, the dashboard reports it on the next start.

WSL, Linux and macOS install in place, because they can replace files that are still open.

### Stopping and restarting, and how long it takes

Every stop is the same operation underneath — Ctrl+C, the reload that follows
**Apply** on a settings page, the restart that finishes an update, and the tray's
**Restart Server**. Since 6.41.0 all of them are bounded by one number,
`SERVER_GRACEFUL_SHUTDOWN_SECONDS` on **Limits & Resilience**, and that number
means what it says:

- At the instant a stop begins the server stops taking new work. A request that
  arrives during the drain gets `503` with `Connection: close` and a
  `Retry-After` hint, so a busy client can no longer keep a closing server alive
  by making ordinary requests.
- Requests already in flight get the whole budget to finish.
- At the bound, whatever is still open is closed. An in-flight stream simply
  ends — there is no special error frame on the wire — and your coding agent's
  own retry finds the restarted server a moment later.
- The process exits a few seconds past the bound whatever is still running.

**The default is 20 seconds.** If you carried `SERVER_GRACEFUL_SHUTDOWN_SECONDS=300`
over from an older install, consider lowering it. Before 6.41.0 this number
bounded only one wait inside the stop and everything after it was unbounded, so
a large value cost nothing in practice; now it is the time you spend watching a
tray icon after pressing Restart.

The tray and the update helper follow the same budget rather than numbers of
their own. The tray asks the server to stop, waits that budget, and only then
terminates the process it launched — by the exact process id, never by name. The
Windows deferred-update helper does the same with the server it is waiting for,
and then installs; it used to poll for a full hour and give up without
installing anything.

### From the command line

Re-running the install command does exactly the same thing and always fetches the newest release.

---

## 14. Security and networking

Worth understanding before you expose anything.

### What binds where

| Surface | Default bind | Access control |
| --- | --- | --- |
| **Proxy API** (`/v1/...`) | `0.0.0.0:8082` | Bearer token, if `ANTHROPIC_AUTH_TOKEN` is set |
| **Admin UI** (`/admin`) | same port | **Loopback callers only**, always |

The proxy binds to **all interfaces** by default, so another machine on your network can reach it. The Admin UI is separately restricted to loopback and cannot be reached remotely regardless of bind address.

### The auth token

`ANTHROPIC_AUTH_TOKEN` ships as `freecc` in `.env.example`. It is compared in constant time against the bearer token your agent sends.

**If you clear it, authentication is disabled entirely** — any caller that can reach the port can spend your provider credits. That is fine on a single-user laptop behind a firewall; it is not fine on a shared or exposed network. Change it from the default if anything other than you can route to the machine.

To bind loopback-only instead, set `HOST=127.0.0.1`.

### What never leaves the machine

Provider API keys are never sent to your agent, never written to the analytics stores, and never included in configuration snapshots — only masked `first4…last4` labels. Proxy credentials are stripped from any recorded URL.

---

## 15. Troubleshooting

**`mcc-server: command not found` right after installing.**
Close and reopen your terminal. The installer extends `PATH`; an existing shell won't see it. This is the single most common install issue.

**Windows: installing while MCC is running.**
Supported, and it is the normal path. The installer renames the old tool environment *and* every `mcc-*` / `fcc-*` launcher aside, then lets uv write a complete new set. Running sessions — the server, the tray, an open `mcc-claude` window — keep the old version until they are restarted; anything started afterwards uses the new one. Nothing needs to be closed.

If Windows refuses to move a launcher aside (an antivirus scan, the search indexer, or the shell can hold an `.exe` for a moment), the installer does not give up: it re-runs uv with `UV_TOOL_BIN_DIR` pointed at a staging directory, so uv writes every shim and a complete receipt somewhere nothing is holding, then places the shims one at a time. A shim that still cannot be replaced keeps the file it had and is listed by name as *"these keep working and will refresh on the next install"* — that is not a failure. A uv launcher is a version-agnostic stub that runs the interpreter inside the tool directory, and that directory now holds the new install, so the old stub already runs the new code.

**Windows: the installer says a command is missing.**
That means a command the release publishes is genuinely absent, not merely stale. Close the `mcc-claude` window(s) and the tray, then re-run the install command. The installer exits non-zero in that case — it never reports "verified" for a command that does not exist.

**Two configs on Windows.**
If you installed under both PowerShell and WSL you have `C:\Users\<you>\.mcc` *and* `~/.mcc` inside WSL. The server prints which config directory it is using at startup — check that against the one you've been editing.

**Claude Code still talks to Anthropic.**
`~/.claude/settings.json` wins over shell exports. Confirm with `/status` — it should show `http://127.0.0.1:8082`. Check the JSON is valid and that you edited the path for your platform.

**401 from the proxy.**
Your agent's `ANTHROPIC_AUTH_TOKEN` doesn't match the server's. Compare the value in `~/.claude/settings.json` — or the Desktop gateway API key — against the server's setting.

**Provider validation fails with 404.**
Usually the model id, not the key. Check the exact id against the provider's model list.

**Claude Desktop's test buttons fail.**
The server must be running for **Test connection** and **Test model discovery** to succeed — they make real calls.

**Desktop shows a warning dialog on launch.**
Expected with model discovery on; the picker fills in once discovery completes.

**Agent can't reach the proxy from another machine.**
The proxy binds `0.0.0.0`, so it should be reachable — check your firewall. The *Admin UI* is loopback-only by design and will refuse remote callers no matter what.

**Web search returns nothing useful.**
Open the attempt detail in Web Search analytics. It shows exactly what was sent upstream and what came back, including whether your domain filters were applied or dropped.

**Cache hit rate shows `—`.**
That provider doesn't report prompt caching. Not a fault — see [Reading the token columns](#reading-the-token-columns).

**`mcc-desktop` hangs or does nothing on WSL.**
There's no tray on WSL/headless — run `mcc-server` instead and open the dashboard from a Windows browser. See [WSL and headless: there is no tray](#wsl-and-headless-there-is-no-tray).

**I changed a `DESKTOP_*` setting and nothing happened.**
It applies on the next `mcc-desktop` launch, not to a tray already running — quit and relaunch. See [DESKTOP_* settings apply on the next launch](#desktop-settings-apply-on-the-next-launch).

**Update did nothing on Windows.**
Versions below 4.21.5 had a defect where the deferred installer could stall. A self-updater can't fix its own updater, so update once from the install script; after that the dashboard button works.

**Anything else.**
The request analytics **View** dialog shows the complete exchange for any request — request body, response, resolved provider and model, timing, and errors. Start there.


---

## Appendix: what changed in 6.x

Only the keys whose value or meaning moved in 6.0.0–6.8.0. Everything else in `.env.example` is unchanged.

| Setting | Default | What it decides |
| --- | --- | --- |
| `CREDENTIAL_LOCKOUT_TIERS` | `300,3600,86400` | The auth lockout ladder, in seconds. A 401/403 takes the next entry each time and stays on the last one. Any comma-separated list of positive seconds works; the field spells your list back at you as *1st auth failure: 5m out · 2nd: 1h out · 3rd and after: 1d out*. |
| `RATE_LIMIT_COOLDOWN_SECONDS` | `60` | Used when a 429 arrives with no `Retry-After` and no equivalent header, and — since 6.34.0 — as the whole-key bench for an account the provider says is out of credits. When a header does arrive, the provider's own number wins. Capped at one hour either way. Named in the `All API keys for this provider are in cooldown` error since 6.16.0, alongside the **Credential health** card that edits it. |
| `CREDENTIAL_MODEL_BENCH_ESCALATION` | `2` | New in 6.19.0. A 429 benches the (key, model) pair, not the key; this is how many different models must be limited on one key at once before the key itself is benched. `1` restores the 6.18.0 whole-key bench, `0` never escalates. |
| `FALLBACK_FIRST_TOKEN_TIMEOUT` | `0` (no limit) | **Changed in 6.16.0** — was `180`. Silence before any output. The only deadline that produces a failover. |
| `FALLBACK_ATTEMPT_SHARE_FLOOR` | `0` (equal share) | **Changed in 6.16.0** — was `180`. Chain-side floor on one model's slice of the total budget. Moot while the budget is `0`. |
| `FALLBACK_TOTAL_TIMEOUT` | `0` (no limit) | **Changed in 6.16.0** — was `600`. The whole request, across every attempt and retry. |
| `FALLBACK_STALL_TIMEOUT` | `0` (no limit) | **Changed in 6.16.0** — was `180`. Silence after output started. Ends the request; no failover is possible past the first token. |
| `FALLBACK_REASONING_ANSWER_TIMEOUT` | `0` (no limit) | **Changed in 6.16.0** — was `450`. Thinking that never becomes an answer. |
| `FALLBACK_COOLDOWN_STEP_OVER_FLOOR` | `5.0` | The shortest remaining rate-limit cooldown that makes stepping over a model worth the chain slot it costs. |
| `PROVIDER_RETRY_BACKOFF_BASE_SECONDS` | `2` | First wait between retries of one model. |
| `PROVIDER_RETRY_BACKOFF_MAX_SECONDS` | `10` | The longest single wait. The chain is not tried until the ladder is spent, and since 6.20.0 only a 5xx or a dropped connection walks it. |
| `PROVIDER_RETRY_ATTEMPTS` | `3` | **Changed in 6.20.0** — was `5`. Tries one model gets on the same key after a 5xx or a dropped connection. A 429 uses none of them. |
| `RATE_LIMIT_ROUTES_AROUND_MODEL` | `true` | **New in 6.20.0.** A 429 benches the (key, model) pair and the request moves to the next model on the same provider instead of retrying and then spending the rest of the key pool. `false` restores retry-then-rotate. |
| `PROVIDER_RETRY_BACKOFF_JITTER_SECONDS` | `1` | Random spread added to it, so several clients do not retry in lockstep. |
| `REQUEST_LOG_WIRE_BODY_MAX_CHARS` | `8000` | Bounds the stored **message and tool structure** only. Parameters are stored whole at any size. |
| `MAX_OUTPUT_TOKENS_CEILING` | **`131072`** | The hard ceiling on `max_tokens`. **`0` means no ceiling**; a blank field means "use the default", not "off". Range `0`–`1048576`. |
| `FALLBACK_BENCH_ENABLED` | `false` | Master switch for model benching. Off (the default since 6.14.0) tries every model in the chain every time; on makes the rest of the Chain benching card live, and only upstream 5xx / overloaded / 401 / 403 count towards a bench. Reachable from **Model Config** as well as this card — one setting, two places. Worth checking if your `.env` predates 6.1.0. |
| `FALLBACK_EJECT_SECONDS` | `30` | How long a benched model stays out. Honoured exactly since 6.0.0; a clamp used to cut it to 1 s for timeout and 5xx ejections. |
| `FALLBACK_END_CLEANLY_AFTER_COMMIT` | `true` | **New in 6.15.0.** A model that fails *after* it started answering ends the message cleanly (`stop_reason: max_tokens`) instead of returning an API error under a partial answer. `false` restores the error. |
| `FALLBACK_RESUME_AFTER_COMMIT` | `true` | **New in 6.18.0.** Rather than only ending a half-written answer, hand the text already sent to the next model on the route and splice its continuation into the same message. Falls back to the row above whenever the continuation is unusable, so it can only lengthen an answer, never break one. `false` stops at the short message. |
| `STREAM_COMMIT_HOLDBACK_CHARS` | `0` | **New in 6.18.0.** Visible characters that must arrive before output is released, on top of `STREAM_COMMIT_HOLDBACK_SECONDS`. Raising it means a model that writes a word and dies has shown you nothing, so the route restarts on the next model invisibly; the cost is that much time-to-first-visible-word on every request. `0` uses the clock alone. |
| `HARNESS_TIER_ALIASES` | `true` | **New in 6.38.0.** Lists `mcc/best`, `mcc/good`, `mcc/medium`, `mcc/cheap` and `mcc/vision` at the top of every coding agent's generated picker, each a name for one of MCC's own routes rather than a model of its own. Off keeps those pickers to concrete refs; the router still resolves an alias a client sends anyway, so an agent already configured on one keeps working. Per-agent chains live in `~/.mcc/harness_tiers.json`, written by the **Coding agents** page. See [Tiers for every other coding agent](#tiers-for-every-other-coding-agent). |
| `SERVER_GRACEFUL_SHUTDOWN_SECONDS` | `20` | **Changed in 6.41.0** — was `300`, and it used to bound only uvicorn's connection wait while the response cleanup, the provider drain and the ASGI lifespan had no bound at all (a request against a silent upstream meant a server that never exited). It is now one deadline for the whole stop, new requests are refused with `503` for its duration, and the process exits a few seconds past it. Lower an inherited `300` unless you would rather wait five minutes for a restart than cut a long request. |
| `CREDENTIAL_CIRCUIT_THRESHOLD` | **removed at 6.0.0** | The circuit breaker it configured no longer exists for provider pools. A stale line is ignored, not fatal — delete it. |

**Settings that moved page, not meaning:** the nine `REQUEST_LOG_*` keys are at the bottom of **Analytics**, the nine `DESKTOP_*` keys are on **Providers**, `LOG_LEVEL` joined the logging flags under **Diagnostics**, and `HTTP_*_TIMEOUT`, `PROVIDER_RATE_LIMIT`, `PROVIDER_RATE_WINDOW` and `PROVIDER_MAX_CONCURRENCY` came *onto* **Limits & Resilience**. `FALLBACK_SKIP_KINDS` stays on **Model Config**, cross-linked, because which failures abort a chain is a routing decision rather than a resilience one.

**Gone from the vocabulary entirely:** the 10/30/60/120-second cooldown ladder, the provider-pool circuit breaker and its half-open probes, per-credential first-token budgets (`MIN_CREDENTIAL_FIRST_TOKEN_SECONDS`), and "consecutive failures" as something that benches a **key**. Consecutive failures still bench a **model**, under `legacy` eject mode.
