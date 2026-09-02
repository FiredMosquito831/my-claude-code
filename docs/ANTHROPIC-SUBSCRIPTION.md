# Using a Claude subscription through MCC

> **This page is the disclaimer. Read it before enabling `anthropic_oauth`.**

MCC can route requests using the OAuth credential from your Claude Pro or Max
subscription, either by discovering the one Claude Code already stored or by
signing in itself.

**Anthropic does not permit this.** Not "discourages", not "is ambiguous about"
— their published documentation names both halves of it directly.

## What Anthropic actually says

From [Claude Code → Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance),
verbatim:

> **OAuth authentication** is intended exclusively for purchasers of Claude
> Free, Pro, Max, Team, and Enterprise subscription plans and is designed to
> support ordinary use of Claude Code and other native Anthropic applications.
>
> **Developers** building products or services that interact with Claude's
> capabilities, including those using the Agent SDK, should use API key
> authentication through Claude Console or a supported cloud provider.
> **Anthropic does not permit third-party developers to offer Claude.ai login
> or to route requests through Free, Pro, or Max plan credentials on behalf of
> their users.**
>
> Anthropic reserves the right to take measures to enforce these restrictions
> and may do so **without prior notice**.

Timeline: the documentation was updated on **19 February 2026**; subscriptions
stopped covering third-party tool usage on **4 April 2026**. Enforcement is
live and is applied at the account level.

## There is no "inside Claude Code" exemption

This is the specific misconception this page exists to correct, because it is
the intuitive one and it is wrong.

It is tempting to reason: *the session is a real Claude Code session, so the
subscription still covers it.* It does not, and the reason is mechanical rather
than legalistic:

```
claude  ──►  127.0.0.1:8082  ──►  MCC  ──►  api.anthropic.com
             (ANTHROPIC_AUTH_TOKEN=freecc)   ▲
                                             └── MCC's HTTP client presents
                                                 YOUR subscription credential
```

Claude Code authenticates to **MCC**. **MCC** then makes the upstream call with
its own HTTP client and headers, presenting your plan credential. That is a
third-party product routing requests through a Max credential, which is the
sentence quoted above, regardless of what launched the session.

Anyone who tells you otherwise — including an earlier version of these docs —
is describing how they wish it worked.

## What MCC does to limit the blast radius

MCC cannot make this permitted. It can refuse to make it *worse*, and it does
one specific thing.

Claude Code stamps an attribution line at the head of the system prompt, inside
the request body:

```
x-anthropic-billing-header: cc_version=2.1.258; cc_entrypoint=cli;
```

Measured on real traffic, four values appear: `cli` (the terminal), `cli-bg`
(the same client running a background task), `sdk-cli` (the Agent SDK driving
the Claude Code binary — this is also what `claude -p` reports) and `sdk-py`
(the Python Agent SDK). Because the marker travels in the body, a proxy can
neither forge it for traffic it did not receive nor strip it from traffic it
did.

**The marker is a good-faith attribution field, not an authenticator.** Its
value is `process.env.CLAUDE_CODE_ENTRYPOINT`, so anything that sets that
variable and reuses Claude Code's system-prompt shape can claim any entrypoint.
The gate narrows who reaches the credential; it does not prove who they are,
and nothing in this document should be read as claiming otherwise.

**The policy this gate enforces: the subscription credential may serve requests
from Anthropic's own clients only — the Claude Code CLI and the Claude Agent
SDK.** Those are the entrypoints `cli`, `cli-bg`, `sdk-cli`, `sdk-py` and
`sdk-ts`. Every other harness routed through MCC — OpenCode, Cline, Crush, a
bare API call — is refused, with a message naming
`ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE` and pointing at the `anthropic` provider
instead.

Before 6.36.0 the gate admitted `cli` alone. On the traffic this was measured
against that refused 64% of genuine Claude Code work, all of it Anthropic's own
Agent SDK — which Anthropic's policy names alongside Claude Code rather than
against it. Widening the gate to the SDK entrypoints is what 6.36.0 changed;
what stayed the same is that everything else is still refused.

The gate is controlled by `ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE` (default
`true`), settable as **Only Serve Claude Code And The Agent SDK** on the Claude
subscription card of the dashboard's Providers page. Turning it off removes
the only structural protection here, and takes effect after a restart.

### What MCC sends upstream

Since 6.36.0 the upstream header set is Claude Code's, with the values mirrored
from the inbound request wherever the client sent its own. Every row was read
out of Claude Code 2.1.258's binary; `providers/anthropic_oauth/auth.py`
carries the byte offsets.

| Header | What MCC sends |
| --- | --- |
| `Authorization` | `Bearer <access token>` |
| `x-api-key` | **never sent** — Claude Code nulls it whenever an OAuth credential is in play |
| `anthropic-version` | the client's value, else `2023-06-01` |
| `anthropic-beta` | MCC's floor (`oauth-2025-04-20`, `claude-code-20250219`) unioned with the client's own list, intersected with a closed allow-list |
| `user-agent` | the client's value, else `claude-cli/2.1.258 (external, cli)` |
| `x-app` | the client's value (`cli` or `cli-bg`), else `cli` |
| `anthropic-dangerous-direct-browser-access` | `true` |

Tool names go upstream verbatim. Releases before 6.36.0 renamed every tool to
`cc_<name>`; no such prefix exists anywhere in Claude Code, and the rename is
gone.

Mirroring is not the same as asserting: every mirrored value is one the client
itself sent. The fallback constants are used only when a request arrived
without them, and the gate refuses those requests anyway.

## What it does not do

Be clear about what the gate is and is not:

- It does **not** make this permitted. It narrows the traffic, nothing more.
- It does **not** hide anything from Anthropic. MCC sends the Claude Code
  header set, including `x-app` and a `claude-cli` User-Agent, because the
  operator chose the full header set. Those headers assert the request came
  from Anthropic's official CLI. The gate is what keeps that assertion true —
  but the assertion is being made by MCC, not by Claude Code.
- It does **not** protect against retry amplification. MCC's three retry layers
  have no shared deadline; a pathological case can produce far more upstream
  attempts than a real client would. Anthropic's stated enforcement concern is
  third-party harnesses generating unusual traffic patterns.
- It cannot protect an account that is already flagged.

## The risk, stated plainly

The risk is to **the Claude account whose credential you use**. Anthropic
states it may enforce without prior notice. Reported consequences for
subscription OAuth used outside Claude Code have included request-level
refusals and account-level disruption.

Nobody can tell you the probability. What is knowable is that the behaviour is
named in the policy, enforcement exists, and it is your account.

## The supported alternative, which is already shipped

MCC has an `anthropic` provider that uses a **Claude Console API key** and is
billed per token. It speaks the same native Messages API, through the same
transport, and carries no policy question at all:

```
ANTHROPIC_API_KEY="sk-ant-..."      # platform.claude.com/settings/keys
MODEL="anthropic/claude-sonnet-4-6"
```

Claude models are also reachable through `bedrock` and `vertex` under
commercial agreements, and resold by several gateways in the catalog
(`kilo`, `nous_portal`, `cline`).

And the two-door pattern still works and disturbs nothing:

```
claude       -> native auth, your subscription, supported
mcc-claude   -> the proxy, for everything else, that session only
```

## Enabling it anyway

If you have read the above and still want it:

```bash
# Option A: use the credential Claude Code already stored (nothing to do --
# MCC discovers ~/.claude/.credentials.json read-only and never rotates it)

# Option B: sign in with a credential MCC owns and can refresh itself
mcc-anthropic-oauth-login
```

Then point a model reference at it:

```
MODEL="anthropic_oauth/claude-sonnet-4-6"
```

### Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE` | `true` | Refuse any request that did not come from Claude Code or the Claude Agent SDK (`cc_entrypoint` in `cli`, `cli-bg`, `sdk-cli`, `sdk-py`, `sdk-ts`) |
| `ANTHROPIC_OAUTH_ACCESS_TOKEN` | *(empty)* | Raw token override, **one value only**. It carries no refresh token, so it **cannot be refreshed** — it will expire and stay expired. A comma-separated list is rejected at construction: several non-refreshing tokens are not a rotation pool. Prefer the login. |
| `ANTHROPIC_OAUTH_UPSTREAM_BASE_URL` | `https://api.anthropic.com/v1` | Upstream override. Deliberately *not* `ANTHROPIC_BASE_URL`, which points Claude Code at MCC. |
| `ANTHROPIC_OAUTH_PROXY` | *(empty)* | HTTP proxy for this provider |

### Credential handling

- MCC's own credential lives at `~/.fcc/anthropic_oauth.json`, mode `0600`.
- Claude Code's file (`~/.claude/.credentials.json`) is **read-only** to MCC and
  never refreshed in place — rotating it would log out your real client.
- Tokens are never written to the request log, an HTTP response, or a log line.
  Neither is a token endpoint's response body, which can echo what was
  presented to it.
- The access token is refreshed **before** it expires, in the background, so a
  request in flight goes out on the credential it already has. Only a genuinely
  expired token makes a request wait. A 401 refreshes once and retries once.
- Refresh is single-flight per credential *file*, so a second MCC process or a
  hot-reloaded provider cannot spend the refresh token twice.
- The request log's `key_label` for this provider is the plan and the
  credential's origin — `max · mcc`, `max · claude-code`. No email: MCC never
  fetches the profile.
