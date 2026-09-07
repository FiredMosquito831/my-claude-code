# OAuth providers

Sign-in-based providers: an Anthropic Claude subscription, ChatGPT, and Kimi For Coding. Moved out of the README in 6.57.0; nothing here changed.

### Anthropic Claude subscription (OAuth, Caution) — not permitted by Anthropic

> **Read [docs/ANTHROPIC-SUBSCRIPTION.md](./ANTHROPIC-SUBSCRIPTION.md) before enabling this.** It is the disclaimer, and this section is only the summary.

MCC can route requests with the OAuth credential from a Claude Pro or Max subscription, discovered from Claude Code's own `~/.claude/.credentials.json` or obtained with `mcc-anthropic-oauth-login`.

**Anthropic's published terms forbid it.** From [Claude Code → Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance):

> Anthropic does not permit third-party developers to offer Claude.ai login or to route requests through Free, Pro, or Max plan credentials on behalf of their users.

There is **no "inside Claude Code" exemption**. Once MCC is interposed, Claude Code authenticates to *MCC*, and *MCC* makes the upstream call with your plan credential — which is exactly the sentence above, whatever launched the session. Anthropic states it may enforce without prior notice, and enforcement is account-level. **The risk is to your Claude account.**

**What MCC does to limit it.** Claude Code stamps an attribution line at the head of the system prompt, inside the request body:

```
x-anthropic-billing-header: cc_version=2.1.258; cc_entrypoint=cli;
```

**The subscription credential may serve requests from Anthropic's own clients only — the Claude Code CLI and the Claude Agent SDK.** Those are the entrypoints `cli`, `cli-bg`, `sdk-cli`, `sdk-py` and `sdk-ts`; every other harness routed through MCC — OpenCode, Cline, Crush, a bare API call — is refused and pointed at the `anthropic` provider instead. Because the marker travels in the body, a proxy can neither forge it for traffic it did not receive nor strip it from traffic it did; it is still a good-faith attribution field rather than an authenticator, since its value is `CLAUDE_CODE_ENTRYPOINT`. Set `ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE=false` to remove that protection — also settable as **Only Serve Claude Code And The Agent SDK** on the Claude subscription card of the **Providers** page (restart required). Until 6.36.0 the gate admitted `cli` alone and refused the Agent SDK, which Anthropic's policy names alongside Claude Code.

Since 6.36.0 the upstream request is Claude Code's own shape: the token goes in `Authorization: Bearer` (never `x-api-key`), `anthropic-beta` is MCC's floor unioned with the client's own list, and `user-agent`, `x-app` and `anthropic-version` are mirrored from the inbound request. Tool names go out verbatim. Before 6.36.0 this provider had never produced a successful request.

MCC's own credential lives at `~/.mcc/anthropic_oauth.json` (mode `0600`). Claude Code's file is read-only to MCC and is never refreshed in place — rotating it would log out your real client. The access token is refreshed ahead of expiry in the background, single-flight per credential file, and a 401 refreshes once and retries once. A raw `ANTHROPIC_OAUTH_ACCESS_TOKEN` works as a single-value override but cannot be refreshed, and a comma-separated list of them is rejected.

The Claude subscription card on the **Providers** page reports the plan, the rate-limit tier, both token expiries, the scopes, and the 5-hour/weekly usage windows — the last of these only when a real Anthropic response carried the header, and otherwise the literal *not yet observed*.

**Signing in, and the two buttons beside it (6.43.0).** *Sign in with Anthropic*
opens a loopback callback when one can work, and falls back on its own to a paste
prompt under WSL, over SSH, and anywhere else localhost is not shared — you can
paste **either the code Anthropic shows you or the whole callback URL out of the
address bar**; both are parsed. `mcc-anthropic-oauth-login` does the same from a
terminal, takes `--paste` and `--no-browser`, answers `--help` without starting
anything, and reports every failure as one line on stderr rather than a traceback.
The card also has **Refresh now** and **Disconnect**, both enabled only once MCC
has a credential of its own. **An import, a sign-in and a Disconnect all take
effect on the next request — no restart.** MCC watches the store's mtime and size
and re-resolves when either moves. (The one thing on this card that *does* need a
restart is `ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE`, which is read when the provider
is constructed.)

**A dead store no longer masks a working credential (6.43.0).** MCC can see two
credentials — its own and Claude Code's — and picks between them **on viability,
not on existence**: if MCC's own access token is expired and its refresh token is
past its stated expiry, it falls through to `~/.claude/.credentials.json` and says
so once in `server.log`, naming both the source it chose and the one it skipped.
Before 6.43.0 the first file holding a non-empty token won even if that token was
years dead, and a stale store masked a healthy Claude Code login permanently.
*On macOS, Claude Code usually keeps its credential in the login keychain instead
of that file, which MCC cannot read — sign in rather than import there.*

**A rate-limited refresh is not a dead credential.** Only `400`, `401` and `403`
*with an OAuth error body* are treated as definitive. A `429` or a `5xx` or a
dropped connection is a transient failure: the stored credential is kept, the
provider reports it as an ordinary rate limit or overload exactly as it would for
any other provider, and **you are not told to sign in again** — signing in again
there would rotate a working credential away for nothing. When a refresh *is*
definitive, the store is **renamed aside to `anthropic_oauth.json.dead-<epoch>`,
never deleted**, so the evidence survives; Claude Code's own file is never touched
either way. *Disconnect* does the same rename.

**What "Caution" on the provider label means.** Not "experimental" — the code
works. It is three standing restrictions: Anthropic's terms forbid this and
enforcement is account-level (above); the credential serves **only** the
`cli`, `cli-bg`, `sdk-cli`, `sdk-py` and `sdk-ts` entrypoints, so every other
harness routed through MCC is refused; and it is **one credential, never a
rotation pool** — a comma-separated `ANTHROPIC_OAUTH_ACCESS_TOKEN` is rejected at
construction, because rotating subscription credentials is the "unusual traffic
pattern" Anthropic's own policy names.

**The supported alternative is already here:** the `anthropic` provider with a [Claude Console API key](https://platform.claude.com/settings/keys), billed per token. Claude models also arrive through `bedrock`, `vertex`, and several gateways. And the two-door pattern still works — native `claude` for your subscription, `mcc-claude` for everything else.

### ChatGPT OAuth Provider (experimental)

MCC can talk directly to `chatgpt.com/backend-api/codex/responses` (OpenAI Responses API) using your ChatGPT subscription's OAuth tokens. Four login paths:

1. **Admin UI → Log in with device code** — the default and recommended path; it works across Windows/WSL, SSH, containers, and other remote environments without a localhost callback.
2. **Admin UI → Browser login (same device)** — browser PKCE for cases where the browser and MCC definitely share the same localhost. Do not use it when MCC runs in WSL and the browser runs on Windows.
3. `mcc-chatgpt-oauth-login` — browser PKCE locally, with immediate device-code fallback under WSL/remote sessions or when the callback cannot start. `--device` forces device login; `--browser` explicitly confirms a same-localhost browser.
4. **Import Codex CLI Tokens** — after `codex login`, copy the complete renewable credential bundle into MCC without modifying `~/.codex/auth.json`.

MCC stores its renewable credentials separately at `~/.mcc/auth/chatgpt-oauth.json`. The Admin API and `.env` contain only a non-secret managed-credential reference. A raw `CHATGPT_OAUTH_ACCESS_TOKEN` remains supported as an advanced override, but it cannot be refreshed.

**The model list is discovered, not hardcoded.** The Codex backend answers `401` on its own models endpoint for an OAuth session, so the catalog cannot come from the gateway. MCC reads the [models.dev](https://models.dev) `openai` catalog it already caches and filters it by the same allowlist the Codex CLI uses — so a new GPT-5.x release appears after a **Refresh models** rather than after a new MCC version. A static list is used when that cache is unavailable, so a fresh offline install still has a usable picker.

Currently exposed: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, `gpt-5.6-luna`, `gpt-5.6-sol`, and `gpt-5.6-terra`. Note that `gpt-5.6` is a family name rather than a callable id on this plan — the bare id returns 404, so only the three named variants are offered. Optional overrides: `CHATGPT_OAUTH_ACCOUNT_ID`, `CHATGPT_OAUTH_BASE_URL`, `CHATGPT_OAUTH_PROXY`.

**ChatGPT OAuth is experimental and unsanctioned.** It is not an official OpenAI API product. The ChatGPT/Codex backend only exposes a limited set of built-in tools, so custom MCC tools may be rejected; use it at your own risk.

### Kimi For Coding Provider

Moonshot's coding-plan endpoint, separate from the standard Kimi platform: OpenAI-compatible at `api.kimi.com/coding/v1`. Set `KIMI_CODING_API_KEY` from [kimi.com/coding](https://kimi.com/coding) and pick a model such as `kimi_coding/kimi-k2.5`.

<a id="connect-your-client"></a>

