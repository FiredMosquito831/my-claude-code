# My Claude Code — Complete Usage Guide

From a fresh machine to a tuned setup: installing, connecting Claude Code and Claude Desktop, adding providers, routing models, web search, and analytics.

The [README](../README.md) is the overview. This is the long-form manual.

---

## Contents

- [1. How it works](#1-how-it-works)
- [2. Install](#2-install)
- [3. First run](#3-first-run)
  - [Running the server with the desktop tray](#running-the-server-with-the-desktop-tray)
  - [Picking a window](#picking-a-window)
  - [Installing the desktop shortcut](#installing-the-desktop-shortcut)
  - [DESKTOP_* settings apply on the next launch](#desktop-settings-apply-on-the-next-launch)
  - [WSL and headless: there is no tray](#wsl-and-headless-there-is-no-tray)
  - [The embedded webview (pywebview), and its caveat](#the-embedded-webview-pywebview-and-its-caveat)
- [4. Tutorial: connect Claude Code (CLI)](#4-tutorial-connect-claude-code-cli)
- [5. Tutorial: connect Claude Desktop](#5-tutorial-connect-claude-desktop)
- [6. Tutorial: connect Codex and Pi](#6-tutorial-connect-codex-and-pi)
- [7. Providers and API keys](#7-providers-and-api-keys)
  - [Using Claude models](#using-claude-models)
- [8. Model tiers and routing](#8-model-tiers-and-routing)
- [9. Web search](#9-web-search)
- [10. Analytics](#10-analytics)
  - [The Token Optimizer page](#the-token-optimizer-page)
- [11. Multi-key rotation](#11-multi-key-rotation)
  - [The RTK token optimizer](#the-rtk-token-optimizer)
- [12. Updating](#12-updating)
- [13. Security and networking](#13-security-and-networking)
- [14. Troubleshooting](#14-troubleshooting)

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

> **Pick one environment and stay in it.** On Windows you can install under PowerShell *or* WSL. Both work — but they keep **separate configs** (`C:\Users\<you>\.fcc` versus `~/.fcc` inside WSL). Installing in both is the most common way to end up editing one config while the server reads the other.
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

The dashboard is where everything is configured. Every setting maps to a variable in `~/.fcc/.env`, and the UI writes to that same file — see [.env.example](../.env.example) for the fully annotated list. If you edit the file by hand, restart the server, because configuration is read at startup.

There is also a **Guide** tab inside the dashboard with a condensed version of this document, available offline.

On first run, the dashboard opens straight to a **Get Started** checklist instead of the Providers tab. It walks through configuring a provider, mapping model tiers, connecting Claude Code, and then points at the optional web search and analytics pages. Dismiss it once you're set up — the Get Started tab stays in the nav if you want it back.

### The two addresses that matter

| What | Default | Who uses it |
| --- | --- | --- |
| **Proxy API** | `http://127.0.0.1:8082` | your coding agent |
| **Admin UI** | `http://127.0.0.1:8082/admin` | you, in a browser |

Same port. The Admin UI is additionally restricted to loopback callers — see [Security and networking](#13-security-and-networking).

### Running the server with the desktop tray

Two optional commands change how the server is *owned*, and both write to the same `~/.fcc/desktop.json` state file:

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

There is no single "native window" API that behaves the same across Windows, macOS, and Linux (WebView2 / WKWebView / WebKitGTK all differ), so `mcc-desktop` resolves its window through a **provider chain** that prefers Chromium **app-mode**: a real browser process launched with no tabs and no URL bar, its own taskbar entry, and a private profile under `~/.fcc/desktop-profile`.

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

<a id="desktop-settings-apply-on-the-next-launch"></a>

### DESKTOP_* settings apply on the next launch

> **These settings apply on the next `mcc-desktop` launch, not to a tray already running.** `mcc-desktop` is a separate process from `mcc-server` and reads them once at start — changing one in the dashboard or in `~/.fcc/.env` does nothing to a tray you already have open. Quit and relaunch `mcc-desktop` to pick it up.

Nine settings live under **Admin → Limits → Desktop**:

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

> **On the token:** it authenticates your agent *to the proxy*, nothing more. It is not a provider key. If you clear `ANTHROPIC_AUTH_TOKEN` on the server, the proxy stops requiring authentication altogether — convenient on a single-user machine, but read [Security and networking](#13-security-and-networking) first.

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

## 6. Tutorial: connect Codex and Pi

Both have launchers that configure the environment for you:

```bash
mcc-codex      # Codex CLI against the local MCC Responses provider
mcc-pi         # Pi
```

(The legacy `fcc-codex` and `fcc-pi` aliases behave identically.)

Codex reads a model catalog that MCC generates, so its own picker works normally:

<div align="center">
  <img src="../assets/codex-model-picker.png" alt="Codex model picker with the generated MCC catalog" width="720">
</div>

<div align="center">
  <img src="../assets/codex.png" alt="Codex CLI running through My Claude Code" width="720">
</div>

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

If you enable it anyway, MCC refuses by default to use the subscription for anything that did not come from the Claude Code CLI — it reads the `cc_entrypoint=cli` marker Claude Code puts in the request body, so an Agent SDK script pointed at your proxy is refused rather than silently billed to your plan.

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

Set the matching variable in `~/.fcc/.env`:

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

**Failover stops once you have seen output.** This is the part people get wrong:

| The model fails… | What happens |
| --- | --- |
| while connecting, authenticating, or rate-limiting | the next model takes over, invisibly |
| before it emits anything | the next model takes over, invisibly |
| halfway through streaming its answer | the request fails |
| at any point, for a **non-streaming** request | the next model takes over — nothing reached you yet |

A chain rescues the failures that happen before the first word, not the ones that happen at word five hundred. Switching models mid-answer would splice two different replies together, so MCC refuses to.

**A model that goes quiet is a failure too.** Accepting a request and then producing nothing looks, to a proxy with no deadline, exactly like thinking hard — so without a limit it holds the request until the transport gives up, and the chain gets its turn minutes later. Three settings on the **Limits** tab bound that:

| Setting | Default | What it does |
| --- | --- | --- |
| First-token deadline | `120s` | How long a model may stay silent before the next one takes over. Nothing has streamed yet, so you never see the switch. |
| Total request budget | `600s` | The whole request, across every attempt and retry. A stream that already started cannot be replaced, but it can be stopped. |
| Eject mode | `rate_based` | How a failing model is benched. `rate_based` (default) skips a model when its failure rate over the last `FALLBACK_EJECT_WINDOW` requests (default 10) crosses `FALLBACK_EJECT_FAILURE_RATE` (default 50%), with at least `FALLBACK_EJECT_MIN_SAMPLES` (default 8) requests observed, for `FALLBACK_EJECT_SECONDS` (default 3). A single blip never benches a working model; sustained failures do. `legacy` preserves the old consecutive-count behavior keyed on `FALLBACK_EJECT_AFTER_FAILURES` / `FALLBACK_EJECT_SECONDS`. |
| Retry primary once | `skip` | What happens when the primary model fails. `skip` (default) moves straight to the next fallback. `retry_once` gives the primary one more chance for transient errors (timeout, 5xx, 429) before falling through. Auth and invalid-request errors are never retried. |

If every model on a route is benched, MCC tries them in order anyway — skipping a bad model is an optimisation, refusing to try anything is an outage.

**Running out of context no longer ends the chain.** A conversation that outgrew a model's window and a genuinely malformed request both come back as HTTP `400`, and until 5.43.0 MCC treated them alike: it gave up on the whole chain, on the reasoning that a bad request will be bad everywhere. That is true of a malformed body and false of a context overflow, which is precisely what a larger-window fallback is for. MCC now tells the two apart and falls through to the next model on an overflow. If you preferred the old behaviour, set `FALLBACK_SKIP_KINDS=invalid_request,context_length` to abort on both again.

Requests that name a provider and model directly (`open_router/…`) are never redirected. An explicit choice is honoured as given.

### The Codex App catalog

The Codex App has no launcher — it reads a persistent `~/.codex/config.toml` rather than an environment built per command. So the server itself owns the model catalog file: `mcc-server` writes `~/.fcc/codex-model-catalog.json` on startup and whenever the model inventory changes, and the Codex App points at that stable path from its config:

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

### Reasoning control

Providers expose reasoning differently. MCC resolves your intent once at the boundary and each provider adapter translates it, so you configure it in one place rather than per provider. See the Model Config tab.

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

Two separate local SQLite stores under `~/.fcc/logs/`, both written by a background thread so they never block a request.

### Model requests

<div align="center">
  <img src="../assets/admin-analytics.png" alt="Model request analytics" width="860">
</div>

Summary cards cover volume, success and error rate, latency percentiles, time-to-first-token and token usage. Below: requests over time, tokens by model, and per-provider and per-key tables.

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
REQUEST_LOG_COMPRESSION_LEVEL=9    # 1-22; 19 measured 4.9% smaller at 9x the time
```

All of these are editable in **Admin UI → Limits** without touching the file. Leaving one blank means "use the default" rather than "invalid", so clearing a field can never stop the server starting, and a value outside its range is refused by the form and clamped (with a warning) if it was edited into the file by hand.

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

### The Token Optimizer page

**Admin UI → Token Optimizer** answers one question from your own request log: what never reached a provider at all? Nothing on this page is switched on for you.

Some requests are answered inside the proxy by a **local rule** — MCC replies and no provider is ever contacted. Those requests show in the request table as **answered locally · <rule>**, not as provider `(unknown)` the way they read before 5.48.0, and you can filter the table by that value to see only them. Because no provider served them, they record no provider, and the tokens they saved are counted from the real request rather than assumed.

The page has four panels:

- **Ledger** — "Tokens never sent": prompt tokens no provider ever received. This is not a bill estimate. What a provider would have charged for the reply cannot be known, and MCC does not guess at it.
- **Local rules** — how often each rule actually fired.
- **Candidates** — recurring request shapes that no rule covers yet, ranked by the tokens they really cost. Press **Scan the log** to produce them. The scan is on demand only: it never runs on a schedule or when the page loads, it reads nothing until you ask, and it changes nothing about how any request is answered. Ask it for more rows than it will scan and it refuses outright instead of quietly sampling and presenting the sample as the whole picture.
- **Cache effectiveness** — prompt-cache hit rate per provider. This is the biggest lever on the page and the optimizer does not control it. A dash means the provider never reported the figure, which is not the same as reporting zero.

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

Each key carries its own state, and only two things change it. A **401/403** locks the key out on an escalating ladder (`CREDENTIAL_LOCKOUT_TIERS`). A **429** benches it for exactly as long as the provider asked. Nothing else does — a timeout, a 5xx, a `410 model gone`, a 400 or a dropped connection leaves every key exactly as it was, because the same keys serve every model in your chain and none of those failures is the key's doing. When a model will not answer, the fallback chain moves to the next **model**.

A **rate-limited key is benched for exactly as long as the provider says** — parsed from `Retry-After`, `retry-after-ms` or `x-ratelimit-reset-*` — rather than an invented fixed delay. A key that resets in one second isn't idled for a minute, and one that needs an hour isn't hammered.

Per-key state, usage and health are visible in the Admin UI, including which key served which request.

### The RTK token optimizer

RTK (the Rust Token Killer, v0.45.0) is an optional third-party binary that filters noisy terminal output before it reaches the model — trimming the token cost of long, chatty agent sessions without changing what the agent does. MCC manages the binary and its per-agent hooks through one command, `mcc-rtk` (legacy alias `fcc-rtk`):

```bash
mcc-rtk status              # installed binary + enabled agents
mcc-rtk enable claude,pi    # install the hook for these agents
mcc-rtk disable codex       # remove the hook for one agent
mcc-rtk uninstall           # disable every agent and remove the binary
mcc-rtk apply               # re-apply the stored state to the machine
```

Enablement is per agent — `claude`, `codex`, and `pi` are each toggled independently. On first enable MCC downloads the pinned RTK release, verifies its SHA-256, installs it under `~/.local/bin`, and patches the agent's own config with telemetry disabled. Desired state lives in `~/.fcc/rtk.json`; `mcc-rtk apply` reconciles the machine against that stored state after any drift.

The same controls live in the dashboard under the **Token optimizer** card and in the `mcc-desktop` tray's **Token optimizer** submenu, so the three surfaces stay in sync.

MCC reads RTK's own `rtk gain` report and shows the resulting savings on the [Token Optimizer page](#the-token-optimizer-page), and tells you when the RTK binary installed on your machine is no longer the version MCC pins.

**RTK telemetry is off when MCC enables it, and MCC cannot turn it on.** MCC patches each agent's config with telemetry disabled and additionally forces RTK's telemetry opt-out environment variable on every invocation, which also short-circuits RTK's own consent prompt. Enabling RTK through MCC therefore cannot opt you into RTK telemetry.

---

## 12. Updating

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

### From the command line

Re-running the install command does exactly the same thing and always fetches the newest release.

---

## 13. Security and networking

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

## 14. Troubleshooting

**`mcc-server: command not found` right after installing.**
Close and reopen your terminal. The installer extends `PATH`; an existing shell won't see it. This is the single most common install issue.

**Two configs on Windows.**
If you installed under both PowerShell and WSL you have `C:\Users\<you>\.fcc` *and* `~/.fcc` inside WSL. The server prints which config directory it is using at startup — check that against the one you've been editing.

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
