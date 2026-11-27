# Remote Control

Drive local coding agents remotely from your phone or another browser using the `/rc` mobile web app (PWA), while keeping `/admin` strictly restricted to loopback (`127.0.0.1`).

---

## What Remote Control is

Remote Control exposes a dedicated mobile-friendly control surface at `/rc` without opening the MCC configuration dashboard (`/admin`) to the network:

- **Opt-in by default:** `RC_ENABLED` defaults to `false`. Until you turn it on, `/rc` answers `503` and no remote pairing or WebSocket endpoints accept sessions.
- **Strict `/admin` isolation:** `/admin` and `/admin/api/*` always require a loopback client address (`127.0.0.1`, `::1`, or `localhost`). Even when a tunnel forwards external HTTPS traffic to MCC, requests arriving through the tunnel are blocked from `/admin`.
- **Signed device cookies + CSRF:** Pairing exchanges a single-use 60-second token (`POST /rc/api/pair`, rate-limited to 8 attempts per 60 seconds) for an HttpOnly HMAC-SHA256 signed `mcc_rc_session` cookie (using `RC_SESSION_SECRET`, auto-generated and persisted under `~/.mcc` when blank) plus an `mcc_rc_csrf` double-submit token and WebSocket `Origin` validation.
- **Fail-closed directory containment:** New sessions and the mobile folder picker (`/rc/api/fs/dirs`) only accept directories inside `RC_ALLOWED_DIRS` (verified with symlink-safe `Path.resolve()` checks). When `RC_ALLOWED_DIRS` is empty, remote path selection is refused.
- **Session cap and instant revocation:** `RC_MAX_SESSIONS` bounds concurrent active sessions (default `12`, range `1` to `64`). Pressing **Revoke all devices** (or **Revoke** on one device) on the **Remote Control** page invalidates device cookies and closes active WebSockets immediately.

---

## Transport comparison: Tailscale Serve vs Tailnet Direct vs Cloudflare Tunnel

MCC supports four ways to reach `/rc`. Choose the transport on the **Remote Control** page (or via `RC_TUNNEL_MODE`):

| Transport | `RC_TUNNEL_MODE` | URL shape | Security & browser support | When to use it |
| --- | --- | --- | --- | --- |
| **Local Loopback** | `off` | `http://127.0.0.1:8082/rc` | Same machine only; zero external exposure. | Local testing, or when you run your own TLS reverse proxy. |
| **Tailscale Serve** *(recommended)* | `tailscale_serve` | `https://<node>.ts.net/rc` | End-to-end WireGuard + Tailnet HTTPS (`tailscale serve --bg`). Keeps `HOST=127.0.0.1` loopback-only and never exposes `/admin`. Enables PWA install, Service Workers, and Web Push alerts. | You already use Tailscale on your workstation and phone. |
| **Tailnet Direct** | `tailscale_local` | `http://100.x.y.z:8082/rc` | Plain HTTP over your Tailnet WireGuard interface. Requires `HOST=0.0.0.0` (with `ANTHROPIC_AUTH_TOKEN` set) if reached directly without `tailscale serve`. Browsers restrict Web Push and PWA install over plain HTTP. | Quick access on your Tailnet when Tailscale Serve is not enabled in your Tailnet ACLs. |
| **Cloudflare Tunnel** | `cloudflare` | `https://*.trycloudflare.com/rc` or your custom hostname | Outbound-only `cloudflared` tunnel with HTTPS. Without a token, starts an ephemeral quick tunnel; with `RC_CLOUDFLARE_TUNNEL_TOKEN` and `RC_CLOUDFLARE_PUBLIC_HOSTNAME`, uses your Zero Trust Named Tunnel. | Your phone is not on Tailscale and you want HTTPS access without opening router ports. |

> **Note:** Clicking **Stop tunnel** on the **Remote Control** page immediately stops an active `cloudflared` process or tears down `tailscale serve`. Stopping `mcc-server` also cleans up any tunnel it started.

---

## Supported harnesses and driver tiers

Remote Control supervises coding agents across structured bidirectional drivers and a full interactive PTY terminal bridge:

| Driver tier | Harnesses | Capabilities |
| --- | --- | --- |
| **Tier 1B (Claude bidirectional)** | `claude` | Persistent bidirectional `stream-json` driver with live streaming text/thinking, inline tool approvals, runtime mode and model switching, and session forking. |
| **Tier 1A (Structured RPC)** | `opencode`, `opencode2`, `kilo`, `codex`, `openclaw` | Structured JSON-RPC / app-server / gateway streams with message turns, tool approval cards, and session controls. |
| **Tier 2 (ACP, Pi RPC, CommandCode)** | `hermes`, `gemini_cli`, `qwen_code`, `kimi_code`, `cline_cli`, `goose`, `droid`, `pi`, `commandcode_cli` | Structured turn events and tool approvals via ACP, Pi RPC, or Command Code structured drivers. |
| **Tier 3 (PTY bridge & `--rc` sidecars)** | `terminal` (shell) or any harness in PTY mode | Live interactive xterm.js terminal over `/rc/ws/sessions/{session_id}/pty`. Any structured session can also be upgraded to PTY mid-flight (`switch-pty`). |

### Additional session features

- **Local session discovery and import:** Inspect and resume existing local agent sessions on the machine (`/rc/api/discovered-sessions`, `/rc/api/sessions/import`) inside `RC_ALLOWED_DIRS`.
- **Opt-in `--rc` terminal dual-sync:** Pass `--rc` to any launcher before `--` (for example `mcc-claude --rc` or `mcc-codex --rc`) to spawn a detached PTY sidecar registered via `/admin/api/rc/sidecars/register`, so you can watch and steer the same live terminal from both your desk and your phone.
- **Git worktrees and diffs:** Optionally isolate a new session in its own git worktree (`create_worktree`) and inspect live repository changes (`/rc/api/sessions/{session_id}/diff`).
- **Live MCC proxy controls:** Change the routed model or tier, reasoning effort, and preferred wire surface for multi-door providers directly from a session (`/rc/api/sessions/{session_id}/proxy-controls`).
- **Push and messenger alerts:** Subscribe mobile browsers to RFC 8291/8292 VAPID Web Push notifications (`/rc/api/push/subscribe`) for approval prompts and completed turns, or forward alerts to Telegram/Discord via `RC_MESSAGING_ALERTS`.

---

## Permission modes

`RC_PERMISSION_MODE` sets the default (and ceiling) permission policy for new Remote Control sessions:

| Mode | Behaviour | Claude driver mapping |
| --- | --- | --- |
| `ask` *(default)* | Prompts in `/rc` before executing write or shell tools. | `--permission-mode default` |
| `auto_edits` | Auto-approves file edit tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, etc.) while still asking on shell commands and external tools. | `--permission-mode acceptEdits` |
| `yolo` | Auto-approves all tool calls without prompting. **High risk — never default; opt in deliberately per session or config.** | `--permission-mode bypassPermissions --allow-dangerously-skip-permissions` |

---

## Pairing and quickstart

1. **Enable and scope `/rc`:** Open the **Remote Control** page in the local dashboard (`http://127.0.0.1:8082/admin`), enable `RC_ENABLED`, set `RC_ALLOWED_DIRS` to the project folder(s) you want remote sessions to access, pick `RC_PERMISSION_MODE` and `RC_TUNNEL_MODE`, and save your settings.
2. **Start a transport (if connecting from another device):** Click **Start Tailscale Serve**, **Use Tailnet Direct**, or **Start Cloudflare Tunnel** on the **Remote Control** page.
3. **Pair your phone:** Click **Generate pairing QR**. Scan the QR code (or copy the `?pair=...` URL, which `/rc` exchanges for a cookie and strips from browser history immediately), or paste the 60-second token into the pairing form.
4. **Launch or attach a session:** Pick a harness and an allowed folder, or attach a discovered/sidecar session (`mcc-claude --rc`).
5. **Tear down when finished:** Click **Stop tunnel** to close external reachability, or **Revoke all devices** to sign out every paired browser immediately.

---

## Settings and environment variables reference

| Variable | Default | Description |
| --- | --- | --- |
| `RC_ENABLED` | `false` | Master switch enabling `/rc`, pairing exchange, session supervision, and `/rc` WebSockets. |
| `RC_PERMISSION_MODE` | `ask` | Default and ceiling permission mode for new sessions (`ask`, `auto_edits`, `yolo`). |
| `RC_ALLOWED_DIRS` | `""` | Comma- or path-separator-delimited workspace roots allowed for sessions and directory browsing. Empty disables remote path selection. |
| `RC_MAX_SESSIONS` | `12` | Maximum concurrent active Remote Control sessions (`1` to `64`). |
| `RC_TUNNEL_MODE` | `off` | Preferred transport (`off`, `tailscale_serve`, `tailscale_local`, `cloudflare`). |
| `RC_TAILSCALE_BINARY` | `""` | Optional explicit path to the `tailscale` executable; blank auto-detects on `PATH` and standard OS paths. |
| `RC_TAILSCALE_HOSTNAME` | `""` | Optional override for the Tailnet DNS name or IP used in pairing URLs. |
| `RC_CLOUDFLARE_BINARY` | `""` | Optional explicit path to the `cloudflared` executable; blank auto-detects on `PATH`. |
| `RC_CLOUDFLARE_TUNNEL_TOKEN` | `""` | Optional Cloudflare Zero Trust Named Tunnel token; blank starts an ephemeral quick `trycloudflare.com` tunnel. |
| `RC_CLOUDFLARE_PUBLIC_HOSTNAME` | `""` | Public HTTPS URL or hostname mapped to your Cloudflare Named Tunnel for QR pairing links. |
| `RC_SESSION_SECRET` | `""` | HMAC-SHA256 signing key for `mcc_rc_session` cookies; blank auto-generates and persists one under `~/.mcc`. |
| `RC_MESSAGING_ALERTS` | `false` | Forward Remote Control approval prompts, turn completions, and errors to your configured Telegram or Discord chat. |

---

## Loopback admin control endpoints

The **Remote Control** page drives these loopback-only admin endpoints:

- `GET /admin/api/rc/status` — current `/rc` state, session counts, and Tailscale/Cloudflare tunnel inspection.
- `POST /admin/api/rc/pair-token` — issue a 60-second single-use pairing token and QR SVG.
- `GET /admin/api/rc/devices` — list paired devices.
- `POST /admin/api/rc/devices/{device_id}/revoke` and `POST /admin/api/rc/devices/revoke-all` — invalidate device sessions and close active WebSockets.
- `POST /admin/api/rc/tunnel/start` and `POST /admin/api/rc/tunnel/stop` — start or stop the active tunnel transport.
- `GET /admin/api/rc/sessions` and `POST /admin/api/rc/sessions/{session_id}/stop` — list or stop supervised sessions.
- `GET /admin/api/rc/sidecars` and `POST /admin/api/rc/sidecars/register` — inspect and register detached `--rc` launcher sidecars.
