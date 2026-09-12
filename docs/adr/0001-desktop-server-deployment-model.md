# ADR 0001 — Desktop / server deployment model

Status: accepted
Date: 2026-08-16

## Context

My Claude Code has two independent ways to run:

- **`mcc-server`** — the headless proxy + admin dashboard. Blocks forever, binds `:8082`. This is the canonical path on WSL / headless Linux / macOS server.
- **`mcc-desktop`** — a system-tray app. Today it *spawns* `mcc-server` as a child when `server_auto_start=true` and `:8082` is free; otherwise it opens the admin dashboard.

The user's requirement: **both modes must coexist without stepping on each other**, and the app must work "serverless" on WSL / Windows via `mcc-server` exactly as before, while still giving desktop users a tray. The deployment model must be explicit and per-platform.

## Decision

We adopt a **three-way server-ownership mode** on the desktop app, and make startup-at-login **per-platform and user-configurable**.

### 1. Server-ownership mode (new `server_mode` field in `~/.mcc/desktop.json`)

| Value | Meaning |
|---|---|
| `spawn` | The desktop app owns `mcc-server` as a child process (current `server_auto_start=true` behavior). For Windows/macOS desktop users. |
| `attach` | The desktop app connects to an existing server on `:8082`, never spawns. For people who run `mcc-server` themselves (WSL/headless/ssh). If nothing is listening, the tray reports "server not running" and offers to open the dashboard (or start it manually). |
| `off` | Tray only; the desktop app does not touch the server at all. |

This replaces the boolean `server_auto_start` (migrated: `server_auto_start=true` → `server_mode="spawn"`, `false` → `server_mode="attach"`).

### 2. Startup-at-login is per-platform

What "start at login" registers is detected from the platform and **user-overridable** in the settings page:

- **Windows** → HKCU `...\CurrentVersion\Run` entry for `mcc-desktop` (tray).
- **macOS** → LaunchAgent for `mcc-desktop` (tray).
- **WSL / Linux** → `systemd --user` unit (preferred) or `~/.config/autostart/*.desktop` for `mcc-server` (headless, no tray).

The settings page detects the running OS (reusing the existing `Origin` detection in `config/claude_discovery.py`: `native_origin()`, `wsl_distributions()`) and shows only the applicable autostart options for the detected platforms.

### 3. `mcc-server` remains unchanged

The headless `mcc-server` command and its behavior are untouched. This preserves the existing WSL / headless / Windows workflow verbatim.

## Consequences

- `config/desktop.py` gains `server_mode` (an enum `spawn|attach|off`) and a migration from `server_auto_start`.
- `cli/desktop.py` `ensure_server()` branches on `server_mode`: `spawn` → spawn child; `attach`/`off` → no spawn (attach just health-checks and reports).
- The dashboard settings page gains a "Deployment" section with `server_mode` and the detected-platform autostart options.
- The desktop tray menu reflects the selected `server_mode` (e.g. "Start server" is only meaningful in `spawn`).
- Autostart application/removal moves into platform-specific helpers that also handle `systemd --user` for Linux.

## Alternatives rejected

- **Desktop-first** (tray owns the server, `mcc-server` deprecated) — breaks the WSL/headless workflow.
- **Server-first** (desktop is a pure remote control, never spawns) — removes the convenience of the tray owning a child for desktop users.
- **Single implicit "spawn-if-down-else-attach"** — cannot express "never spawn" or "tray only".
